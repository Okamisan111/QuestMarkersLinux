using System;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using QuestMarkersLinux;

// Test harness for the production feed server (compiled unchanged from
// plugin/src/WsServer.cs). Emits a minimal but realistically shaped frame so
// the Python integration test (tests/test_plugin_ws.py) exercises the REAL
// challenge/response, Origin rejection, header caps and takeover logic.
internal static class Harness
{
    // Test fixture value only - a public-repo test constant, never a real secret.
    internal const string Token = "HARNESS-TEST-TOKEN-0000000000000000";
    internal const int Port = 19411;

    /// The per-frame tok stamp exactly as plugin/src/Plugin.cs builds it:
    /// HMAC-SHA256(token, frame timestamp) - never the token itself.
    private static string Stamp(long feedTime)
    {
        using (var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(Token)))
        {
            byte[] mac = hmac.ComputeHash(Encoding.UTF8.GetBytes(
                feedTime.ToString(System.Globalization.CultureInfo.InvariantCulture)));
            var sb = new StringBuilder(mac.Length * 2);
            foreach (byte b in mac) sb.Append(b.ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
            return sb.ToString();
        }
    }

    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "labels") return Labels();
        using (var server = new WsServer(Port, Token, msg =>
        {
            Console.WriteLine("[server] " + msg);
            Console.Out.Flush();
        }))
        {
            server.Start();
            Console.WriteLine("HARNESS-READY");
            Console.Out.Flush();
            DateTime deadline = DateTime.UtcNow.AddSeconds(75);
            long i = 0;
            while (DateTime.UtcNow < deadline)
            {
                long t = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
                server.Send("{\"t\":" + t +
                    ",\"v\":[2560,1440],\"cfg\":{\"labels\":true},\"tok\":\"" + Stamp(t) +
                    "\",\"m\":[{\"k\":\"quest\",\"nx\":0.5,\"ny\":0.4,\"b\":0,\"op\":1," +
                    "\"l\":\"Harness\",\"d\":123}]}");
                i++;
                Thread.Sleep(25);
            }
            Console.WriteLine("HARNESS-DONE frames=" + i);
        }
        return 0;
    }

    /// `ws-harness labels`: reads "label<TAB>count" lines and prints exactly the
    /// labels plugin/src/ObjectiveLabels.cs produces for that many world targets
    /// of one quest - the same production code path tests/test_plugin_labels.py
    /// checks, without Unity or BepInEx.
    private static int Labels()
    {
        string line;
        while ((line = Console.ReadLine()) != null)
        {
            if (line.Length == 0) continue;
            string[] parts = line.Split('\t');
            int count;
            if (parts.Length < 2 || !int.TryParse(parts[1],
                    System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture, out count))
            {
                Console.WriteLine("BAD\t" + line);
                continue;
            }
            foreach (string l in QuestMarkersLinux.ObjectiveLabeler.Labels(parts[0], count))
                Console.WriteLine(l);
            // third column: also report the wire-budget clamp of the NAME
            if (parts.Length >= 3)
            {
                int maxChars;
                if (int.TryParse(parts[2], System.Globalization.NumberStyles.Integer,
                        System.Globalization.CultureInfo.InvariantCulture, out maxChars))
                    Console.WriteLine("clamp:" + QuestMarkersLinux.ObjectiveLabeler.Clamp(parts[0], maxChars));
            }
            Console.WriteLine("--");
        }
        return 0;
    }
}
