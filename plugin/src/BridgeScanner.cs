using System;
using System.Collections.Generic;
using System.Text;
using Comfort.Common;
using CommonAssets.Scripts.Game;
using EFT;
using EFT.Interactive;
using EFT.Interactive.SecretExfiltrations;
using EFT.Quests;
using EFT.SynchronizableObjects;
using EFT.Vehicle;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace QuestMarkersLinux;

internal sealed class MarkerTarget
{
    public string Id;
    public Vector3 Position;
    public string Label;
    public string Kind;
    public string State;
    public bool Quest;
    public Transform Anchor;
    public float Lift;
}

/// Scans quest objectives, extractions, airdrops, BTR and quest items from the
/// live game data; mirrors the data model of the marker HUD feed.
internal sealed class BridgeScanner
{
    private readonly Action<string> logDebug;
    private readonly Action<string> logWarning;
    private readonly List<MarkerTarget> targets = new List<MarkerTarget>();
    private readonly Dictionary<string, Vector3> zonesById = new Dictionary<string, Vector3>(StringComparer.Ordinal);
    private readonly Dictionary<string, string> wantedItems = new Dictionary<string, string>(StringComparer.Ordinal);
    private readonly List<(string name, int index)> questExits = new List<(string, int)>();
    private readonly HashSet<string> reportedCategoryConditions = new HashSet<string>(StringComparer.Ordinal);
    private bool zonesScanned;
    private int zoneScanAttempts;
    private int scannedSceneCount = -1;
    private bool warnedThisRaid;

    public BridgeScanner(Action<string> logDebug, Action<string> logWarning)
    {
        this.logDebug = logDebug;
        this.logWarning = logWarning;
    }

    public bool ShowQuests = true;
    public bool ShowExtracts = true;
    public bool ShowAirdrops = true;
    public bool ShowBtr = true;

    public IReadOnlyList<MarkerTarget> Targets => targets;

    public bool InRaid
    {
        get
        {
            if (!Singleton<GameWorld>.Instantiated) return false;
            GameWorld world = Singleton<GameWorld>.Instance;
            if (world == null || world is HideoutGameWorld) return false;
            AbstractGame game = Singleton<AbstractGame>.Instance;
            if (game == null || game.Status != GameStatus.Started) return false;
            Player main = world.MainPlayer;
            return main != null && main.IsYourPlayer;
        }
    }

    public void Clear()
    {
        targets.Clear();
        zonesById.Clear();
        wantedItems.Clear();
        zonesScanned = false;
        zoneScanAttempts = 0;
        scannedSceneCount = -1;
        reportedCategoryConditions.Clear();
        warnedThisRaid = false;
    }

    public void Refresh()
    {
        targets.Clear();
        wantedItems.Clear();
        questExits.Clear();
        try
        {
            GameWorld world = Singleton<GameWorld>.Instance;
            Player player = world?.MainPlayer;
            if (player != null)
            {
                if (ShowQuests && (int)player.Side != 4)
                {
                    AddQuestObjectives(player);
                    AddQuestItems(world, player);
                }
                if (ShowExtracts) AddExtracts(world, player);
                if (ShowAirdrops) AddAirdrops(world);
                if (ShowBtr) AddBtr(world, player);
            }
        }
        catch (Exception ex)
        {
            targets.Clear();
            if (!warnedThisRaid)
            {
                warnedThisRaid = true;
                logWarning("Quest scan failed this raid (" + ex.GetType().Name + ": " + ex.Message + ").");
            }
        }
    }

    private void AddQuestObjectives(Player player)
    {
        if (player.QuestController?.Quests == null) return;
        if (!zonesScanned || SceneManager.sceneCount != scannedSceneCount) ScanZones();
        foreach (Quest quest in player.QuestController.Quests)
        {
            if (quest == null) continue;
            int status = (int)quest.QuestStatus;
            if (status != (int)EQuestStatus.Started && status != (int)EQuestStatus.AvailableForFinish) continue;
            if (quest.Conditions == null) continue;
            if (!quest.Conditions.TryGetValue(EQuestStatus.AvailableForFinish, out ConditionCollection conds) || conds == null) continue;
            string label = Escape(quest.Template?.Name ?? "Quest");
            foreach (Condition condition in conds)
            {
                AddCondition(quest, condition, label);
            }
        }
    }

    private void AddCondition(Quest quest, Condition condition, string label)
    {
        if (condition == null) return;
        try
        {
            if (!quest.CheckVisibilityStatus(condition) || IsDone(quest, condition)) return;
            if (condition is ConditionLeaveItemAtLocation plant)
            {
                AddZone(condition, plant.zoneId, label, "plant");
            }
            else if (condition is ConditionPlaceBeacon beacon)
            {
                AddZone(condition, beacon.zoneId, label, "beacon");
            }
            else if (condition is ConditionZone zone)
            {
                AddZone(condition, zone.zoneId, label, "zone");
            }
            else if (condition is ConditionLaunchFlare flare)
            {
                AddZone(condition, flare.zoneID, label, "flare");
            }
            else if (condition is ConditionVisitPlace visit)
            {
                AddZone(condition, visit.target, label, "zone");
            }
            else if (condition is ConditionInZone inZone)
            {
                if (inZone.zoneIds != null)
                {
                    foreach (string zoneId in inZone.zoneIds) AddZone(condition, zoneId, label, "zone");
                }
            }
            else if (condition is ConditionExitName exit)
            {
                AddExit(condition, exit.exitName, label);
            }
            else if (condition is ConditionFindItem find)
            {
                if (find.target == null) return;
                if (find.TargetIsCategory)
                {
                    if (reportedCategoryConditions.Add(condition.id.ToString()))
                        logDebug("'" + label + "' wants an item category; no world marker for that.");
                    return;
                }
                foreach (string text in find.target)
                {
                    if (string.IsNullOrEmpty(text)) continue;
                    if (wantedItems.TryGetValue(text, out string existing))
                    {
                        if (existing != label && !existing.Contains(label))
                            wantedItems[text] = existing + " / " + label;
                    }
                    else wantedItems[text] = label;
                }
            }
            else if (condition is ConditionCounterCreator counter && counter.Conditions != null)
            {
                foreach (Condition sub in counter.Conditions) AddCondition(quest, sub, label);
            }
        }
        catch (Exception ex)
        {
            logDebug("Skipped a condition of '" + label + "': " + ex.Message);
        }
    }

    private bool IsDone(Quest quest, Condition condition)
    {
        if (condition.IsNecessary && quest.CompletedConditions != null && !quest.CompletedConditions.Contains(condition.id))
            return false;
        try { return quest.IsConditionDone(condition); }
        catch { return false; }
    }

    private void AddZone(Condition condition, string zoneId, string label, string kind)
    {
        if (string.IsNullOrEmpty(zoneId)) return;
        if (zonesById.TryGetValue(zoneId, out Vector3 pos))
        {
            targets.Add(new MarkerTarget
            {
                Id = Escape("z:" + condition.id + ":" + zoneId),
                Position = pos,
                Label = label,
                Kind = Escape(kind),
                Quest = true
            });
        }
    }

    private void AddExit(Condition condition, string exitName, string label)
    {
        if (string.IsNullOrEmpty(exitName)) return;
        ExfiltrationController controller = Singleton<GameWorld>.Instance?.ExfiltrationController;
        if (controller?.ExfiltrationPoints == null) return;
        foreach (ExfiltrationPoint point in controller.ExfiltrationPoints)
        {
            if (point != null && point.Settings != null && point.Settings.Name == exitName)
            {
                questExits.Add((exitName, targets.Count));
                Bounds bounds = BoundsFor(point);
                targets.Add(new MarkerTarget
                {
                    Id = Escape("x:" + condition.id + ":" + exitName),
                    Position = bounds.center,
                    Label = label,
                    Kind = "exit",
                    Quest = true
                });
            }
        }
    }

    private void AddExtracts(GameWorld world, Player player)
    {
        ExfiltrationController controller = world.ExfiltrationController;
        if (controller == null || player.Profile == null || controller.IsMyPlayerBanned()) return;
        if ((int)player.Side == 4) // Scav
        {
            if (controller.ScavExfiltrationPoints != null)
            {
                foreach (ScavExfiltrationPoint point in controller.ScavExfiltrationPoints)
                {
                    if (point != null && point.EligibleIds != null && point.EligibleIds.Contains(player.ProfileId))
                        AddExtract(point);
                }
            }
            ExfiltrationPoint[] secret = controller.GetScavSecretExits();
            if (secret != null)
            {
                foreach (ExfiltrationPoint point in secret) AddExtract(point);
            }
            return;
        }
        ExfiltrationPoint[] eligible = controller.EligiblePoints(player.Profile);
        if (eligible != null)
        {
            foreach (ExfiltrationPoint point in eligible) AddExtract(point);
        }
        SecretExfiltrationPoint[] secrets = controller.SecretEligiblePoints();
        if (secrets != null)
        {
            foreach (SecretExfiltrationPoint point in secrets) AddExtract(point);
        }
    }

    private void AddExtract(ExfiltrationPoint point)
    {
        if (point == null || point.Settings == null || string.IsNullOrEmpty(point.Settings.Name)) return;
        string state = StateOf(point);
        if (state == null) return;
        string name = point.Settings.Name;
        bool merged = false;
        for (int i = 0; i < questExits.Count; i++)
        {
            if (questExits[i].name == name)
            {
                targets[questExits[i].index].State = state;
                merged = true;
            }
        }
        if (!merged)
        {
            Bounds bounds = BoundsFor(point);
            targets.Add(new MarkerTarget
            {
                Id = Escape("e:" + name),
                Position = bounds.center,
                Label = Escape(Localized(name)),
                Kind = "extract",
                State = state
            });
        }
    }

    private static string StateOf(ExfiltrationPoint point)
    {
        EExfiltrationStatus status = point.Status;
        if ((int)status == (int)EExfiltrationStatus.Countdown) return "countdown";
        bool rolled = !(point is ScavExfiltrationPoint) || point is SharedExfiltrationPoint;
        if (rolled && point.Settings.Chance < 100f) return "locked";
        switch ((int)status)
        {
            case (int)EExfiltrationStatus.RegularMode:
            case (int)EExfiltrationStatus.AwaitsManualActivation:
                return NeedsScav(point) ? "locked" : "open";
            case (int)EExfiltrationStatus.UncompleteRequirements:
            case (int)EExfiltrationStatus.Pending:
                return "locked";
            default:
                return null;
        }
    }

    private static bool NeedsScav(ExfiltrationPoint point)
    {
        ExfiltrationRequirement[] reqs = point.Requirements;
        if (reqs == null) return false;
        foreach (ExfiltrationRequirement req in reqs)
        {
            if (req != null && (int)req.Requirement == 10) return true;
        }
        return false;
    }

    private void AddAirdrops(GameWorld world)
    {
        SynchronizableObjectLogicProcessor processor = world.SynchronizableObjectLogicProcessor;
        if (processor == null) return;
        foreach (SynchronizableObject obj in processor.GetSynchronizableObjects())
        {
            if (obj is AirdropSynchronizableObject airdrop && obj.IsActive && obj.IsInited)
            {
                targets.Add(new MarkerTarget
                {
                    Id = "a:" + obj.ObjectId,
                    Position = airdrop.transform.position,
                    Anchor = airdrop.transform,
                    Lift = 1.2f,
                    Label = "Airdrop",
                    Kind = "airdrop"
                });
            }
        }
    }

    private void AddBtr(GameWorld world, Player player)
    {
        BTRView view = world.BtrController?.BtrView;
        if (view == null) return;
        if ((int)view.RouteState != 0) return;
        EPlayerBtrState state = player.BtrState;
        if ((int)state == 2 || (int)state == 3 || (int)state == 4) return;
        targets.Add(new MarkerTarget
        {
            Id = "btr",
            Position = view.transform.position,
            Anchor = view.transform,
            Lift = 3f,
            Label = "BTR",
            Kind = "btr"
        });
    }

    private void AddQuestItems(GameWorld world, Player player)
    {
        if (wantedItems.Count == 0 || world.LootItems == null) return;
        string profileId = player.ProfileId;
        for (int i = 0; i < world.LootItems.Count; i++)
        {
            LootItem loot = world.LootItems.GetByIndex(i);
            if (loot == null || loot.Item == null || !loot.Item.QuestItem) continue;
            if (!loot.IsValidForProfile(profileId)) continue;
            if (loot.TemplateId != null && wantedItems.TryGetValue(loot.TemplateId, out string label))
            {
                targets.Add(new MarkerTarget
                {
                    Id = Escape("i:" + loot.ItemId),
                    Position = loot.transform.position,
                    Label = label,
                    Kind = "item",
                    Quest = true
                });
            }
        }
    }

    private void ScanZones()
    {
        Dictionary<string, Bounds> found = new Dictionary<string, Bounds>(StringComparer.Ordinal);
        TriggerWithId[] triggers = UnityEngine.Object.FindObjectsOfType<TriggerWithId>(true);
        foreach (TriggerWithId trigger in triggers)
        {
            if (trigger == null || string.IsNullOrEmpty(trigger.Id)) continue;
            Bounds b = BoundsFor(trigger);
            if (found.TryGetValue(trigger.Id, out Bounds existing))
            {
                existing.Encapsulate(b);
                found[trigger.Id] = existing;
            }
            else found[trigger.Id] = b;
        }
        zonesById.Clear();
        foreach (KeyValuePair<string, Bounds> kv in found) zonesById[kv.Key] = kv.Value.center;
        scannedSceneCount = SceneManager.sceneCount;
        zoneScanAttempts++;
        zonesScanned = zonesById.Count > 0 || zoneScanAttempts >= 10;
        logDebug("Zone scan found " + zonesById.Count + " trigger ids in " + scannedSceneCount + " scenes (attempt " + zoneScanAttempts + ").");
    }

    private static Bounds BoundsFor(Component trigger)
    {
        Bounds result = new Bounds(trigger.transform.position, Vector3.zero);
        if (!trigger.gameObject.activeInHierarchy) return result;
        foreach (Collider collider in trigger.GetComponents<Collider>())
        {
            if (collider != null && collider.enabled) result.Encapsulate(collider.bounds);
        }
        return result;
    }

    private static string Localized(string text)
    {
        try { return text.Localized(); } catch { return text; }
    }

    internal static string Escape(string text)
    {
        if (string.IsNullOrEmpty(text)) return "";
        bool clean = true;
        foreach (char c in text)
        {
            if (c == '"' || c == '\\' || c < ' ') { clean = false; break; }
        }
        if (clean) return text;
        var sb = new StringBuilder(text.Length + 8);
        foreach (char c in text)
        {
            if (c == '"' || c == '\\') sb.Append('\\').Append(c);
            else if (c < ' ') sb.Append(' ');
            else sb.Append(c);
        }
        return sb.ToString();
    }
}
