using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Threading;

namespace QuestMarkersLinux;

/// Minimal RFC6455 WebSocket server for the native overlay feed.
/// Text frames only; one client slot.
///
/// Feed authentication (protocol v4): the shared token is NEVER sent - not in
/// the URL, not in a header, not in any frame. After the 101 upgrade the
/// server sends a challenge text frame
///   {"c":"chal","n":"<server nonce hex>",
///    "p":"<HMAC-SHA256(key=token, msg="server|"+n) hex>"}
/// The overlay verifies p with the shared token (proving the SERVER - a local
/// process squatting the port cannot forge it) and answers within 8 s with
///   {"c":"auth","r":"<HMAC-SHA256(key=token, msg="client|"+n) hex>"}
/// (proving the CLIENT). The two proofs are domain-separated: a peer that only
/// echoes the server's own p back is rejected, so the answer can only verify
/// for a peer that really holds the token. Both compares are constant time.
/// Only an authenticated client ever occupies the client slot - takeover by a
/// new connection happens strictly after its challenge succeeds, and a client
/// that merely connects without replying holds nothing (8 s auth deadline,
/// 60 s idle timeout afterwards; every client runs on its own thread so the
/// accept loop is never stalled). An empty Feed Token disables the challenge
/// entirely - the plugin logs this loudly.
internal sealed class WsServer : IDisposable
{
    private readonly int port;
    private readonly string token;
    private readonly Action<string> log;
    private TcpListener listener;
    private Thread acceptThread;
    private readonly object sendLock = new object();
    private NetworkStream client;
    private readonly byte[] sendBuf = new byte[8192];
    private int droppedFrames;

    public bool HasClient
    {
        get
        {
            lock (sendLock)
            {
                return client != null;
            }
        }
    }

    public WsServer(int port, string token, Action<string> log)
    {
        this.port = port;
        this.token = token ?? "";
        this.log = log;
    }

    public void Start()
    {
        listener = new TcpListener(IPAddress.Loopback, port);
        listener.Start();
        acceptThread = new Thread(acceptLoop) { IsBackground = true, Name = "QuestMarkersLinux-accept" };
        acceptThread.Start();
        log("Feed WebSocket server listening on 127.0.0.1:" + port);
    }

    private void acceptLoop()
    {
        while (true)
        {
            TcpClient tcp;
            try
            {
                tcp = listener.AcceptTcpClient();
            }
            catch
            {
                return; // listener disposed
            }
            // Each client on its own thread: a client that connects and stays
            // silent can never stall the accept loop (previously HandleClient
            // ran inline and ate the single slot - exactly the documented
            // takeover path was unreachable).
            Thread t = new Thread(() =>
            {
                try
                {
                    HandleClient(tcp);
                }
                catch (Exception ex)
                {
                    log("WebSocket client failed: " + ex.Message);
                }
                finally
                {
                    try { tcp.Close(); } catch { }
                }
            })
            { IsBackground = true, Name = "QuestMarkersLinux-client" };
            t.Start();
        }
    }

    private void HandleClient(TcpClient tcp)
    {
        tcp.NoDelay = true;
        // Send() runs on the Unity main thread. Without a send timeout a
        // wedged/slow overlay client could block Update() (and thus the
        // game) until the OS buffer gives up; 1 s bounds the stall and the
        // existing catch drops the client instead.
        tcp.SendTimeout = 1000;
        tcp.ReceiveTimeout = 10000;
        NetworkStream stream = tcp.GetStream();
        // Whole-handshake deadline: ReceiveTimeout bounds a single read, so
        // a slow-loris drip (one byte every 9 s) would still crawl forever.
        DateTime deadline = DateTime.UtcNow.AddSeconds(12);
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        int ch;
        var lineBuf = new StringBuilder();
        bool first = true;
        int headerBytes = 0;
        while ((ch = stream.ReadByte()) != -1)
        {
            if (DateTime.UtcNow > deadline)
            {
                log("WebSocket handshake dropped: slow (dripping) handshake.");
                return;
            }
            if (ch == '\r') continue;
            if (ch == '\n')
            {
                string line = lineBuf.ToString();
                lineBuf.Clear();
                if (line.Length == 0) break;
                if (first) { first = false; continue; }
                int sep = line.IndexOf(':');
                if (sep > 0) headers[line.Substring(0, sep).Trim()] = line.Substring(sep + 1).Trim();
                continue;
            }
            lineBuf.Append((char)ch);
            // Without these limits any local process could OOM the game process
            // by streaming header bytes that never end a line.
            if (lineBuf.Length > 8192 || ++headerBytes > 65536)
            {
                log("WebSocket handshake dropped: oversized request headers.");
                return;
            }
        }
        if (headers.Count > 64) return;
        if (headers.ContainsKey("Origin"))
        {
            // Browsers always send Origin on a WS handshake (RFC6455 §4.1); the
            // native overlay client never does. This walls drive-by web pages off
            // the feed and off the single-client slot (WS handshakes are NOT CORS-gated).
            log("WebSocket handshake rejected: browser Origin header present " +
                "(the feed is for the native overlay only).");
            try
            {
                byte[] deny = Encoding.ASCII.GetBytes("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
                stream.Write(deny, 0, deny.Length);
            }
            catch { }
            return;
        }
        if (!headers.TryGetValue("Sec-WebSocket-Key", out string key)) return;
        string accept = ComputeAccept(key);
        byte[] resp = Encoding.ASCII.GetBytes("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n");
        stream.Write(resp, 0, resp.Length);

        // ---- feed authentication (v3 challenge/response) ----
        bool authenticated = string.IsNullOrEmpty(token);
        if (!string.IsNullOrEmpty(token))
        {
            byte[] nonceBuf = new byte[16];
            using (RandomNumberGenerator rng = RandomNumberGenerator.Create()) rng.GetBytes(nonceBuf);
            string nonce = Hex(nonceBuf);
            string proof = HmacHex(token, "server|" + nonce);
            SendText(stream, "{\"c\":\"chal\",\"n\":\"" + nonce + "\",\"p\":\"" + proof + "\"}");
            stream.ReadTimeout = 8000; // the overlay answers in milliseconds
            string reply = ReadClientTextFrame(stream, 4096);
            string answer = ExtractJsonField(reply, "r");
            // Domain-separated client proof: an echo of the server's own p must
            // NOT be accepted (that would authenticate anyone who can connect).
            string expected = HmacHex(token, "client|" + nonce);
            if (answer == null || !ConstantTimeEquals(answer, expected))
            {
                log("Feed client refused: challenge response missing or wrong - " +
                    "is the overlay's config.json feedToken the same value as the " +
                    "plugin's Feed Token? (or is some other process connecting?)");
                try { CloseWithCode(stream, 1008); } catch { }
                return; // never touches the current client slot
            }
            authenticated = true;
        }

        // .NET forbids ReadTimeout = 0 - timeouts here are plain millisecond values.
        // Idle: the overlay's websocket keepalive pings arrive every ~20 s; a
        // client that falls silent for 60 s is dropped and the slot frees up.
        stream.ReadTimeout = 60000;

        lock (sendLock)
        {
            NetworkStream prev = client;
            client = stream;
            droppedFrames = 0;
            if (prev != null)
            {
                try { prev.Close(); } catch { }
                log("Overlay client replaced (takeover by the newest client).");
            }
        }
        log(authenticated
            ? "Overlay client connected: feed token verified via challenge."
            : "Overlay client connected WITHOUT feed token (Feed Token is empty - any local process may read the feed).");

        // Read loop: consume frames so the client can keep sending; react to close/ping.
        try
        {
            while (true)
            {
                int b0 = stream.ReadByte();
                int b1 = stream.ReadByte();
                if (b0 < 0 || b1 < 0) break;
                int op = b0 & 0x0F;
                bool masked = (b1 & 0x80) != 0;
                long len = b1 & 0x7F;
                if (len == 126)
                {
                    byte[] ext = new byte[2];
                    ReadFull(stream, ext, 2);
                    len = (ext[0] << 8) | ext[1];
                }
                else if (len == 127)
                {
                    byte[] ext = new byte[8];
                    ReadFull(stream, ext, 8);
                    len = 0;
                    for (int i = 0; i < 8; i++) len = (len << 8) | ext[i];
                }
                if (op >= 0x8 && len > 125) break; // control frames are <=125 bytes (RFC6455 5.5)
                byte[] mask = null;
                if (masked)
                {
                    mask = new byte[4];
                    ReadFull(stream, mask, 4);
                }
                byte[] payload = null;
                if (len > 0 && len < (1 << 20))
                {
                    payload = new byte[(int)len];
                    ReadFull(stream, payload, (int)len);
                    if (mask != null)
                        for (int i = 0; i < payload.Length; i++) payload[i] ^= mask[i & 3];
                }
                else if (len > 0) break; // client payload over 1 MiB: hostile
                if (op == 0x8) break; // close
                if (op == 0x9) Pong(stream, payload);
            }
        }
        catch { } // idle timeout, socket death, protocol garbage
        lock (sendLock)
        {
            if (client == stream) client = null;
        }
        log("Overlay client disconnected.");
    }

    private static void ReadFull(NetworkStream s, byte[] buf, int count)
    {
        int off = 0;
        while (off < count)
        {
            int n = s.Read(buf, off, count - off);
            if (n <= 0) throw new EndOfStreamException();
            off += n;
        }
    }

    private void Pong(NetworkStream stream, byte[] payload)
    {
        // RFC6455 demands echoing the ping payload in an unmasked pong frame.
        // Missing it makes every standards-compliant client (the overlay uses
        // python websockets) kill the connection at its keepalive timeout -
        // that was the repeated "keepalive ping timeout" feed dropouts.
        lock (sendLock)
        {
            if (client != stream) return;
            try
            {
                int n = payload == null ? 0 : payload.Length; // control frames are <=125 bytes
                byte[] head = n < 126
                    ? new byte[] { 0x8A, (byte)n }
                    : new byte[] { 0x8A, 126, (byte)(n >> 8), (byte)n };
                stream.Write(head, 0, head.Length);
                if (n > 0) stream.Write(payload, 0, n);
            }
            catch { }
        }
    }

    public void Send(string json)
    {
        lock (sendLock)
        {
            if (client == null) return;
            byte[] payload = Encoding.UTF8.GetBytes(json);
            byte[] header = BuildFrame(payload.Length);
            int total = header.Length + payload.Length;
            if (total > sendBuf.Length)
            {
                droppedFrames++;
                if (droppedFrames % 300 == 1) log("Frame too large to send (" + total + " bytes), dropped.");
                return;
            }
            Buffer.BlockCopy(header, 0, sendBuf, 0, header.Length);
            Buffer.BlockCopy(payload, 0, sendBuf, header.Length, payload.Length);
            try
            {
                client.Write(sendBuf, 0, total);
            }
            catch (Exception ex)
            {
                client = null;
                log("Send failed, client dropped: " + ex.Message);
            }
        }
    }

    private static void SendText(NetworkStream stream, string text)
    {
        byte[] payload = Encoding.UTF8.GetBytes(text);
        byte[] header = BuildFrame(payload.Length);
        stream.Write(header, 0, header.Length);
        stream.Write(payload, 0, payload.Length);
    }

    /// Read one masked client frame; return its text payload, or null on
    /// timeout/close/non-text (the caller treats null as refused authentication).
    private static string ReadClientTextFrame(NetworkStream stream, int maxLen)
    {
        try
        {
            int b0 = stream.ReadByte();
            int b1 = stream.ReadByte();
            if (b0 < 0 || b1 < 0) return null;
            if ((b0 & 0x0F) != 0x1) return null;            // must be a text frame
            if ((b0 & 0x80) == 0) return null;              // client frames MUST be masked (RFC6455 5.1)
            long len = b1 & 0x7F;
            if (len == 126)
            {
                byte[] ext = new byte[2];
                ReadFull(stream, ext, 2);
                len = (ext[0] << 8) | ext[1];
            }
            else if (len == 127) return null;
            if (len <= 0 || len > maxLen) return null;
            byte[] mask = new byte[4];
            ReadFull(stream, mask, 4);
            byte[] payload = new byte[(int)len];
            ReadFull(stream, payload, (int)len);
            for (int i = 0; i < payload.Length; i++) payload[i] ^= mask[i & 3];
            return Encoding.UTF8.GetString(payload);
        }
        catch
        {
            return null; // timeout (8 s), early close, malformed framing
        }
    }

    /// Extracts a flat string field from the tiny auth reply object
    /// {"c":"auth","r":"<hex>"} - no JSON parser dependency in the plugin.
    /// Tolerates whitespace styles (json.dumps default emits `"r": "..."` with
    /// a space, compact clients emit `"r":"..."`).
    private static string ExtractJsonField(string json, string field)
    {
        if (json == null) return null;
        int at = json.IndexOf("\"" + field + "\"", StringComparison.Ordinal);
        if (at < 0) return null;
        int colon = json.IndexOf(':', at + field.Length + 2);
        if (colon < 0) return null;
        int quote = json.IndexOf('"', colon + 1);
        if (quote < 0) return null;
        int end = json.IndexOf('"', quote + 1);
        if (end <= quote + 1) return null;
        return json.Substring(quote + 1, end - quote - 1);
    }

    private static void CloseWithCode(NetworkStream stream, ushort code)
    {
        byte[] body = { (byte)(code >> 8), (byte)code };
        stream.Write(new byte[] { 0x88, 2 }, 0, 2);
        stream.Write(body, 0, 2);
    }

    private static byte[] BuildFrame(int len)
    {
        var h = new List<byte>();
        h.Add(0x80 | 0x1); // FIN + text
        if (len < 126) h.Add((byte)len);
        else if (len < 65536)
        {
            h.Add(126);
            h.Add((byte)(len >> 8));
            h.Add((byte)len);
        }
        else
        {
            h.Add(127);
            for (int shift = 56; shift >= 0; shift -= 8) h.Add((byte)(len >> shift));
        }
        return h.ToArray();
    }

    private static string ComputeAccept(string key)
    {
        using (SHA1 sha = SHA1.Create())
            return Convert.ToBase64String(sha.ComputeHash(Encoding.ASCII.GetBytes(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11")));
    }

    private static string HmacHex(string key, string message)
    {
        using (var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(key)))
            return Hex(hmac.ComputeHash(Encoding.UTF8.GetBytes(message)));
    }

    private static string Hex(byte[] bytes)
    {
        var sb = new StringBuilder(bytes.Length * 2);
        foreach (byte b in bytes) sb.Append(b.ToString("x2"));
        return sb.ToString();
    }

    private static bool ConstantTimeEquals(string a, string b)
    {
        if (a == null || b == null) return false;
        // net48 has no CryptographicOperations.FixedTimeEquals. Both inputs are
        // fixed-width hex digests, so the LENGTH is not a secret; the content
        // compare below runs over every character without an early exit.
        if (a.Length != b.Length) return false;
        int diff = 0;
        for (int i = 0; i < a.Length; i++) diff |= (ushort)a[i] ^ (ushort)b[i];
        return diff == 0;
    }

    public void Dispose()
    {
        try { listener?.Stop(); } catch { }
        lock (sendLock)
        {
            try { client?.Close(); } catch { }
            client = null;
        }
    }
}
