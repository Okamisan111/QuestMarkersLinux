using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using BepInEx;
using BepInEx.Configuration;
using EFT;
using EFT.CameraControl;
using UnityEngine;

namespace QuestMarkersLinux;

/// Streams quest/extraction/airdrop/BTR markers to the native quest-overlay
/// presenter over a local WebSocket feed (same frame contract as the file
/// feed written by feed_sim.py).
[BepInPlugin(PluginGuid, PluginName, PluginVersion)]
public class QuestMarkersLinuxPlugin : BaseUnityPlugin
{
    public const string PluginGuid = "com.chaossan.questmarkerslinux";
    public const string PluginName = "QuestMarkersLinux";
    public const string PluginVersion = "1.0.0";

    private ConfigEntry<bool> bridgeOn;
    private ConfigEntry<KeyboardShortcut> toggleKey;
    private ConfigEntry<MarkerSet> toggleShows;
    private ConfigEntry<KeyboardShortcut> secondKey;
    private ConfigEntry<MarkerSet> secondShows;
    private ConfigEntry<bool> autoShow;
    private ConfigEntry<int> port;
    private ConfigEntry<string> feedToken;
    private ConfigEntry<float> feedHz;
    private ConfigEntry<float> maxDistance;
    private ConfigEntry<int> maxMarkers;
    private ConfigEntry<bool> showLabels;
    private ConfigEntry<bool> showDistance;
    private ConfigEntry<bool> edgeArrows;
    private ConfigEntry<float> autoHideSeconds;
    private ConfigEntry<float> markerScale;
    private ConfigEntry<float> farDistance;
    private ConfigEntry<float> farScale;
    private ConfigEntry<bool> imperialUnits;
    private ConfigEntry<int> edgeArrowSize;
    private ConfigEntry<float> iconScale;
    private ConfigEntry<bool> debugLog;

    private BridgeScanner scanner;
    private WsServer server;
    private bool lastInRaid;
    private bool peeking;
    private bool secondKeyPeek;
    private float shownSeconds;
    private float nextScan;
    private float nextSend;
    private int sentFrameCount;
    private int clientEverConnected;
    private int noClientWarn;
    private int wireDropped;

    private static readonly StringBuilder json = new StringBuilder(4096);

    private void Awake()
    {
        bridgeOn = Config.Bind("General", "Enabled", true,
            Tagged("Enabled", 90, "Stream the marker feed to the native overlay."));
        toggleKey = Config.Bind("General", "Toggle key", new KeyboardShortcut(KeyCode.I),
            Tagged("Toggle Key", 50, "Shows or hides the markers ticked below during a raid."));
        toggleShows = Config.Bind("General", "Toggle key shows", MarkerSet.Quests | MarkerSet.Extracts | MarkerSet.Airdrops | MarkerSet.BTR,
            Tagged("Toggle Key Shows", 40, "What the toggle key marks, and what appears on its own when a raid starts. Quests: your open objectives. Extracts: the exits this raid gives you, with their state. Airdrops: crates, followed live. BTR: the BTR while it is out on its route."));
        secondKey = Config.Bind("General", "Second key", KeyboardShortcut.Empty,
            Tagged("Second Key", 30, "A second key with its own list below - out of the box the extracts alone. Not bound until you pick a key. Pressing the other key while markers are up switches to its list; the same key again hides them."));
        secondShows = Config.Bind("General", "Second key shows", MarkerSet.Extracts,
            Tagged("Second Key Shows", 20, "What the second key marks."));
        autoShow = Config.Bind("General", "Show in raid automatically", true,
            Tagged("Show At Raid Start", 10, "Show the toggle key's markers whenever a raid starts. Both keys work either way."));
        port = Config.Bind("General", "Feed Port", 9378,
            Tagged("Feed Port", 5, "WebSocket port the quest-overlay presenter connects to (loopback only). Only for non-standard setups.", null, true));
        feedToken = Config.Bind("General", "Feed Token", "",
            Tagged("Feed Token", 3, "Shared secret for the overlay feed: the overlay must connect with ?token=<value> and every frame carries the token back; frames with a wrong token are dropped by the overlay. Empty disables the token check (handshakes carrying a browser Origin header stay rejected either way). Changing the token only takes effect with the game closed: the plugin reads it at game start, the overlay reads its config.json at overlay start - set both sides to the same value, then start the game first, then the overlay.", null, true));
        feedHz = Config.Bind("General", "Feed Hz", 60f,
            Tagged("Feed Hz", 4, "Frames per second pushed to the overlay (1-180, game-frame-aligned sampling). Higher is smoother near quest targets - their screen speed scales with 1/distance; values above your game FPS add nothing.", null, true));
        maxDistance = Config.Bind("Filter", "Maximum Distance", 0f,
            Tagged("Maximum Distance (m)", 10, "Hide markers farther away than this many meters, extracts included. 0 shows everything.",
                new AcceptableValueRange<float>(0f, 2000f)));
        maxMarkers = Config.Bind("Filter", "Maximum Quest Markers", 16,
            Tagged("Maximum Quest Markers", 20, "At most this many quest markers at once; the closest objectives win. Extracts do not count towards this.",
                new AcceptableValueRange<int>(1, 50)));

        showLabels = Config.Bind("Display", "Show Labels", true,
            Tagged("Show Labels", 80, "Show the marker name next to the pin."));
        showDistance = Config.Bind("Display", "Show Distance", true,
            Tagged("Show Distances", 70, "Show the distance (meters/km, or feet/miles in Imperial mode)."));
        edgeArrows = Config.Bind("Display", "Edge Arrows", true,
            Tagged("Edge Arrows For Off-Screen Targets", 60, "Off-screen and behind-the-player markers appear as arrows at the screen edge - never as a marker in the middle of the screen."));
        autoHideSeconds = Config.Bind("Display", "Auto-hide after", 6f,
            Tagged("Auto-Hide After (s)", 50, "Hide the markers again this many seconds after they appeared, so they stay a quick glance rather than permanent clutter. 0 keeps them visible until toggled off.",
                new AcceptableValueRange<float>(0f, 60f)));
        markerScale = Config.Bind("Display", "Marker Scale", 1.0f,
            Tagged("Marker Size", 40, "Overall size of the markers (0.5 = half size, 1.0 = default size).",
                new AcceptableValueRange<float>(0.2f, 2f)));
        farDistance = Config.Bind("Display", "Far Distance", 300f,
            Tagged("Distance For Smallest Size (m)", 20, "The distance in meters at which a marker has shrunk to 'Size At Distance'.",
                new AcceptableValueRange<float>(25f, 1000f), true));
        farScale = Config.Bind("Display", "Far Scale", 0.6f,
            Tagged("Size At Distance", 30, "How large a far-away marker is compared to a close one. 1 keeps every marker the same size.",
                new AcceptableValueRange<float>(0.4f, 1f), true));
        imperialUnits = Config.Bind("Display", "Imperial Units", false,
            Tagged("Imperial Units", 15, "Display feet and miles instead of meters and kilometers."));
        edgeArrowSize = Config.Bind("Display", "Edge Arrow Size", 24,
            Tagged("Edge Arrow Size", 12, "Edge arrow size in pixels at scale 1.0.", null, true));
        iconScale = Config.Bind("Display", "Icon Scale", 1.0f,
            Tagged("Icon Scale", 35, "Size multiplier of the symbol (pin and kind icon).",
                new AcceptableValueRange<float>(0.2f, 5f), true));
        debugLog = Config.Bind("Debug", "Debug Log", false,
            Tagged("Debug Log", 100, "Verbose bridge logging: scan results, frame stats, feed client state - written into the BepInEx log.", null, true));

        scanner = new BridgeScanner(
            msg => { if (debugLog.Value) Logger.LogInfo("[scan] " + msg); else Logger.LogDebug(msg); },
            msg => Logger.LogWarning(msg));
        server = new WsServer(port.Value, feedToken.Value, msg => Logger.LogInfo(msg));
        try
        {
            server.Start();
        }
        catch (Exception ex)
        {
            Logger.LogError("Feed server could not start: " + ex.Message);
        }
        Logger.LogInfo(PluginName + " loaded; overlay connects to ws://127.0.0.1:" + port.Value);
        if (string.IsNullOrEmpty(feedToken.Value))
            Logger.LogWarning("Feed Token is empty: the feed accepts ANY local process " +
                "(live raid positions can be read by anything on this machine). Set a Feed " +
                "Token and put the same value in the overlay's config.json as \"feedToken\".");
    }

    private void Update()
    {
        if (!bridgeOn.Value) return;
        bool inRaid = scanner.InRaid;
        if (inRaid != lastInRaid)
        {
            lastInRaid = inRaid;
            if (inRaid)
            {
                if (autoShow.Value) startPeek(false);
                Logger.LogInfo("Raid started; feed " +
                    (autoShow.Value ? "showing (toggle key hides)." : "idle (toggle key shows)."));
            }
            else
            {
                peeking = false;
                scanner.Clear();
                Logger.LogInfo("Raid ended; feed empty frames.");
            }
        }
        if (inRaid)
        {
            bool first = isPressed(toggleKey.Value);
            bool second = isPressed(secondKey.Value);
            if (first && second)
                press(secondKey.Value.Modifiers.Count() > toggleKey.Value.Modifiers.Count());
            else if (first)
                press(false);
            else if (second)
                press(true);
            if (peeking)
            {
                if (autoHideSeconds.Value > 0f)
                {
                    shownSeconds += Time.unscaledDeltaTime;
                    if (shownSeconds >= autoHideSeconds.Value)
                        endPeek();
                }
                if (Time.unscaledTime >= nextScan)
                {
                    nextScan = Time.unscaledTime + 2f;
                    MarkerSet shows = secondKeyPeek ? secondShows.Value : toggleShows.Value;
                    scanner.ShowQuests = (shows & MarkerSet.Quests) != 0;
                    scanner.ShowExtracts = (shows & MarkerSet.Extracts) != 0;
                    scanner.ShowAirdrops = (shows & MarkerSet.Airdrops) != 0;
                    scanner.ShowBtr = (shows & MarkerSet.BTR) != 0;
                    scanner.Refresh();
                    if (debugLog.Value)
                        Logger.LogInfo(string.Format("[debug] scan: {0} targets, set {1}",
                            scanner.Targets == null ? 0 : scanner.Targets.Count, shows));
                }
            }
        }
        float hz = Math.Max(1f, Math.Min(180f, feedHz.Value));
        if (Time.unscaledTime >= nextSend)
        {
            nextSend = Time.unscaledTime + 1f / hz;
            if (server.HasClient)
            {
                json.Length = 0;
                BuildFrame(json, peeking && inRaid ? scanner.Targets : null);
                server.Send(json.ToString());
                sentFrameCount++;
                if (clientEverConnected == 0)
                {
                    clientEverConnected = 1;
                    Logger.LogInfo("Feed streaming to overlay.");
                }
                if (debugLog.Value && sentFrameCount % 150 == 0)
                    Logger.LogInfo(string.Format("[debug] frame {0}: peeking {1}, markers {2}",
                        sentFrameCount, peeking ? 1 : 0,
                        peeking && inRaid && scanner.Targets != null ? scanner.Targets.Count : 0));
            }
            else if (inRaid && ++noClientWarn >= (int)Math.Max(1, (int)feedHz.Value) * 10)
            {
                noClientWarn = 0;
                Logger.LogInfo("[notice] no overlay client on ws://127.0.0.1:" + port.Value +
                    " - start it before/during the raid: python3 overlay.py --feed ws://127.0.0.1:" + port.Value);
            }
        }
    }

    private void press(bool second)
    {
        if (peeking && secondKeyPeek == second)
            endPeek();
        else
            startPeek(second);
    }

    private void startPeek(bool second)
    {
        secondKeyPeek = second;
        peeking = true;
        shownSeconds = 0f;
        nextScan = 0f;
        if (debugLog.Value)
            Logger.LogInfo(string.Format("[debug] peek on ({0})", second ? "second key" : "toggle key"));
    }

    private void endPeek()
    {
        if (!peeking) return;
        peeking = false;
        if (debugLog.Value)
            Logger.LogInfo("[debug] peek off");
    }

    private static bool isPressed(KeyboardShortcut shortcut)
    {
        if (shortcut.MainKey == KeyCode.None) return false;
        if (!Input.GetKeyDown(shortcut.MainKey)) return false;
        foreach (KeyCode modifier in shortcut.Modifiers)
            if (!Input.GetKey(modifier)) return false;
        return true;
    }

    private void OnDestroy()
    {
        server?.Dispose();
    }

    /// Feed frame v2: {"t":epoch_ms,"v":[W,H],"cfg":{...},"m":[{k,nx,ny,b,op,l,d,s}]}
    /// nx/ny are normalized viewport coords (0..1 on-screen; off-screen values leave
    /// the range and already point toward the correct edge, y top-down); markers
    /// behind the camera are mirrored so nx/ny point to the matching screen edge,
    /// b=1 marks them. d is meters. Edge arrows are derived by the overlay
    /// (cfg "edge"), not emitted by the game side. cfg mirrors the Display config.
    private void BuildFrame(StringBuilder sb, IReadOnlyList<MarkerTarget> targets)
    {
        Camera camera = CameraManager.Exist ? CameraManager.Instance.Camera : null;
        int w = Screen.width;
        int h = Screen.height;
        StringBuilder mJson = new StringBuilder(2048);
        if (camera != null && targets != null)
        {
            Vector3 camPos = camera.transform.position;
            // filter + sort by distance, project to viewport
            visible.Clear();
            for (int i = 0; i < targets.Count; i++)
            {
                MarkerTarget t = targets[i];
                Vector3 pos = t.Position;
                if (t.Anchor != null)
                {
                    if (!t.Anchor.gameObject.activeInHierarchy) continue;
                    pos = t.Anchor.position + Vector3.up * t.Lift;
                }
                float dist = Vector3.Distance(camPos, pos);
                if (maxDistance.Value > 0f && dist > maxDistance.Value) continue;
                Vector3 vp = camera.WorldToViewportPoint(pos);
                if (!IsFinite(dist)) continue;
                if (!IsFinite(vp.x) || !IsFinite(vp.y) || !IsFinite(vp.z))
                {
                    // target sits exactly in the camera side plane: the viewport
                    // projection explodes to NaN/Infinity and used to DROP the
                    // marker - icons vanished well before the auto-hide time at
                    // certain camera angles. Derive the edge placement from the
                    // horizontal direction instead so the marker stays visible
                    // as an edge arrow (same mirror convention the behind path
                    // uses for real projections).
                    Vector3 dir = pos - camPos;
                    float fd = Vector3.Dot(dir, camera.transform.forward);
                    float rd = Vector3.Dot(dir, camera.transform.right);
                    float ud = Vector3.Dot(dir, camera.transform.up);
                    float den = fd >= 0f ? Mathf.Max(fd, 0.2f) : Mathf.Min(fd, -0.2f);
                    vp.x = 0.5f + 0.5f * rd / den;
                    vp.y = 0.5f + 0.5f * ud / den;
                    vp.z = fd;   // sign drives the behind flag and mirroring
                }
                visible.Add((t, vp, dist));
            }
            visible.Sort((a, b) => a.dist.CompareTo(b.dist));
            int questCount = 0;
            int wireBytes = 0;
            // 8192 sendBuf minus WS header (~14) and the fixed frame
            // skeleton (t/v/c/cfg/q + quotes ~160 B) minus the tok field.
            int wireBudget = 8192 - 174
                - (feedToken.Value != null ? Encoding.UTF8.GetByteCount(feedToken.Value) : 0);
            for (int i = 0; i < visible.Count; i++)
            {
                MarkerTarget t = visible[i].t;
                Vector3 vp = visible[i].vp;
                if (t.Quest && t.State == null)
                {
                    if (questCount >= maxMarkers.Value) continue;
                    questCount++;
                }
                bool behind = vp.z < 0f;
                float nx = vp.x, ny = 1f - vp.y;
                if (behind)
                {
                    // mirror the behind-camera projection so the overlay's edge
                    // derivation aims the arrow at the correct screen side
                    nx = 1f - nx;
                    ny = 1f - ny;
                }
                if (!IsFinite(nx) || !IsFinite(ny)) continue;
                // Wire budget in BYTES (WsServer.sendBuf is 8192 and a too
                // large frame would be dropped as a whole): labels can be
                // multi-byte UTF-8, so measure their real UTF-8 length.
                // `visible` is distance-ascending -> stopping here keeps the
                // CLOSEST markers instead of losing the entire frame.
                int est = 44 + (t.Label != null ? Encoding.UTF8.GetByteCount(t.Label) : 0)
                            + (t.State != null ? Encoding.UTF8.GetByteCount(t.State) : 0);
                if (wireBytes + est > wireBudget)
                {
                    wireDropped++;
                    if (wireDropped % 300 == 1)
                        Logger.LogWarning($"Feed frame wire budget reached; " +
                            $"{visible.Count - i} farther targets dropped this frame.");
                    break;
                }
                wireBytes += est;
                if (mJson.Length > 0) mJson.Append(',');
                mJson.Append("{\"k\":\"").Append(t.Kind).Append("\",\"nx\":")
                    .Append(nx.ToString("F4", CultureInfo.InvariantCulture))
                    .Append(",\"ny\":").Append(ny.ToString("F4", CultureInfo.InvariantCulture))
                    .Append(",\"b\":").Append(behind ? 1 : 0)
                    .Append(",\"op\":1,\"l\":");
                AppendJsonString(mJson, t.Label != null && t.Label.Length > 120
                    ? t.Label.Substring(0, 119) + "\u2026" : t.Label);
                mJson.Append(",\"d\":").Append((int)Math.Round(visible[i].dist));
                if (t.State != null)
                {
                    mJson.Append(",\"s\":");
                    AppendJsonString(mJson, t.State);
                }
                mJson.Append('}');
            }
        }
        sb.Append("{\"t\":").Append(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());
        sb.Append(",\"v\":[").Append(w).Append(',').Append(h).Append(']');
        sb.Append(",\"cfg\":{\"labels\":").Append(showLabels.Value ? "true" : "false");
        sb.Append(",\"distance\":").Append(showDistance.Value ? "true" : "false");
        sb.Append(",\"edge\":").Append(edgeArrows.Value ? "true" : "false");
        sb.Append(",\"scale\":").Append(markerScale.Value.ToString("F2", CultureInfo.InvariantCulture));
        sb.Append(",\"farDistance\":").Append(farDistance.Value.ToString("F1", CultureInfo.InvariantCulture));
        sb.Append(",\"farScale\":").Append(farScale.Value.ToString("F2", CultureInfo.InvariantCulture));
        sb.Append(",\"imperial\":").Append(imperialUnits.Value ? "true" : "false");
        sb.Append(",\"edgeSize\":").Append(edgeArrowSize.Value);
        sb.Append(",\"iconScale\":").Append(iconScale.Value.ToString("F2", CultureInfo.InvariantCulture));
        sb.Append('}');
        sb.Append(",\"tok\":");
        AppendJsonString(sb, feedToken.Value ?? "");
        sb.Append(",\"m\":[").Append(mJson).Append(']');
        sb.Append('}');
    }

    private static bool IsFinite(float f) => !float.IsNaN(f) && !float.IsInfinity(f);

    // scratch buffer reused across frames
    private readonly List<(MarkerTarget t, Vector3 vp, float dist)> visible = new List<(MarkerTarget, Vector3, float)>();

    /// SPT's F12 menu lives in BepInEx/plugins/spt/ConfigurationManager/ConfigurationManager.dll.
    /// Verified from its decompile: it inspects ConfigDescription.Tags for either the string tags
    /// "Advanced"/"Hidden"/"Unbrowsable"/"Browsable"/"ReadOnly" (exact match), or a tag object
    /// whose type NAME is exactly "ConfigurationManagerAttributes" - then copies its PUBLIC
    /// FIELDS by name onto SettingEntryBase (DispName, Order, IsAdvanced, ShowRangeAsPercent).
    /// A wrong class name or properties-instead-of-fields is silently ignored - which is why the
    /// previous duck-typed TagAttributes never hid any advanced setting.
    private static ConfigDescription Tagged(string dispName, int order, string description,
        AcceptableValueBase acceptableValues = null, bool advanced = false)
    {
        object[] tags = advanced
            ? new object[] { new ConfigurationManagerAttributes(dispName, order, true), "Advanced" }
            : new object[] { new ConfigurationManagerAttributes(dispName, order, false) };
        return new ConfigDescription(description, acceptableValues, tags);
    }

    internal class ConfigurationManagerAttributes
    {
        public string DispName;
        public int Order;
        public bool IsAdvanced;
        public bool ShowRangeAsPercent;

        public ConfigurationManagerAttributes(string dispName, int order, bool isAdvanced)
        {
            DispName = dispName;
            Order = order;
            IsAdvanced = isAdvanced;
            ShowRangeAsPercent = false;
        }
    }

    private static void AppendJsonString(StringBuilder sb, string s)
    {
        sb.Append('"');
        if (s != null)
        {
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                else if (c >= ' ') sb.Append(c);
                else sb.Append(' ');
            }
        }
        sb.Append('"');
    }
}

/// Which marker kinds a key marks; serialized as "Quests, Extracts" style lists in the
/// config file (same shape the original QuestMarkers mod uses).
[Flags]
public enum MarkerSet
{
    None = 0,
    Quests = 1,
    Extracts = 2,
    Airdrops = 4,
    BTR = 8,
}
