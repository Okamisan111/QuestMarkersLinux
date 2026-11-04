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
/// Single client; new connections replace the old one. Text frames only.
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
            TcpClient tcp = null;
            try
            {
                tcp = listener.AcceptTcpClient();
            }
            catch
            {
                return; // listener disposed
            }
            try
            {
                HandleClient(tcp);
            }
            catch (Exception ex)
            {
                log("WebSocket client handshake failed: " + ex.Message);
            }
            tcp?.Close();
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
        // The accept loop calls HandleClient inline: a client that connects
        // but never sends the upgrade request would stall the loop forever
        // (no new overlay could ever connect). Bound the pre-upgrade reads;
        // after the handshake the read loop is intentionally unbounded.
        tcp.ReceiveTimeout = 10000;
        NetworkStream stream = tcp.GetStream();
        // Read the HTTP upgrade request line-by-line.
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        int ch;
        var lineBuf = new StringBuilder();
        bool first = true;
        string requestLine = "";
        int headerBytes = 0;
        while ((ch = stream.ReadByte()) != -1)
        {
            if (ch == '\r') continue;
            if (ch == '\n')
            {
                string line = lineBuf.ToString();
                lineBuf.Clear();
                if (line.Length == 0) break;
                if (first) { first = false; requestLine = line; continue; }
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
        if (!string.IsNullOrEmpty(token))
        {
            string given = QueryToken(requestLine);
            if (given == null || !ConstantTimeEquals(given, token))
            {
                log("WebSocket handshake rejected: missing/wrong feed token " +
                    "(overlay must connect ws://127.0.0.1:" + port + "/?token=<Feed Token>).");
                try
                {
                    byte[] deny = Encoding.ASCII.GetBytes("HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n");
                    stream.Write(deny, 0, deny.Length);
                }
                catch { }
                return;
            }
        }
        if (!headers.TryGetValue("Sec-WebSocket-Key", out string key)) return;
        string accept = ComputeAccept(key);
        byte[] resp = Encoding.ASCII.GetBytes("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n");
        stream.Write(resp, 0, resp.Length);
        // Post-upgrade: wait for frames/pings indefinitely. .NET forbids
        // ReadTimeout = 0 - the sentinel for "no timeout" is Timeout.Infinite.
        stream.ReadTimeout = System.Threading.Timeout.Infinite;

        NetworkStream prev;
        lock (sendLock)
        {
            prev = client;
            client = stream;
            droppedFrames = 0;
        }
        if (prev != null) log("Overlay client replaced (new connection).");
        log("Overlay client connected.");

        // Read loop: consume frames so the client can keep sending; react to close/ping.
        try
        {
            while (true)
            {
                int b0 = stream.ReadByte();
                int b1 = stream.ReadByte();
                if (b0 < 0 || b1 < 0) break;
                int op = b0 & 0x0F;
                long len = b1 & 0x7F;
                if (len == 126)
                {
                    len = (stream.ReadByte() << 8) | stream.ReadByte();
                }
                else if (len == 127)
                {
                    len = 0;
                    for (int i = 0; i < 8; i++) len = (len << 8) | stream.ReadByte();
                }
                byte[] mask = new byte[4];
                ReadFull(stream, mask, 4);
                byte[] payload = null;
                if (len > 0 && len < 1 << 20)
                {
                    payload = new byte[(int)len];
                    ReadFull(stream, payload, (int)len);
                    for (int i = 0; i < payload.Length; i++) payload[i] ^= mask[i & 3];
                }
                else if (len > 0) break;
                if (op == 0x8) break; // close
                if (op == 0x9) Pong(stream, payload);
            }
        }
        catch { }
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

    private static string QueryToken(string requestLine)
    {
        if (string.IsNullOrEmpty(requestLine)) return null;
        string[] parts = requestLine.Split(' ');
        if (parts.Length < 2) return null;
        string path = parts[1];
        int q = path.IndexOf('?');
        if (q < 0) return null;
        foreach (string pair in path.Substring(q + 1).Split('&'))
        {
            int eq = pair.IndexOf('=');
            if (eq <= 0) continue;
            if (pair.Substring(0, eq) == "token")
                return Uri.UnescapeDataString(pair.Substring(eq + 1));
        }
        return null;
    }

    private static bool ConstantTimeEquals(string a, string b)
    {
        if (a == null || b == null || a.Length != b.Length) return false;
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
