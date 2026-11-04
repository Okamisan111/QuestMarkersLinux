using System;
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

    private static int Main()
    {
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
                server.Send("{\"t\":" + DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() +
                    ",\"v\":[2560,1440],\"cfg\":{\"labels\":true},\"tok\":\"" + Token +
                    "\",\"m\":[{\"k\":\"quest\",\"nx\":0.5,\"ny\":0.4,\"b\":0,\"op\":1," +
                    "\"l\":\"Harness\",\"d\":123}]}");
                i++;
                Thread.Sleep(25);
            }
            Console.WriteLine("HARNESS-DONE frames=" + i);
        }
        return 0;
    }
}
