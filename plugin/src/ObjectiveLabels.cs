using System;
using System.Collections.Generic;

namespace QuestMarkersLinux;

/// Quest objective labels are the quest NAME - and one quest can carry several
/// world objectives. The backported "Signal - Part 1" has four (two VisitPlace
/// zones, two PlaceBeacon zones), so the feed shows four boxes that all read
/// "Signal - Part 1" with different meters. Standing still and watching one
/// spot on screen, the box that lands there changes when you move, which reads
/// as "the same marker suddenly shows a different distance".
///
/// Multi-objective quests therefore get a stable objective index appended:
/// "Signal - Part 1 (2/4)". The index is the condition enumeration order of
/// that quest, so it does not depend on distance, camera angle or which markers
/// the wire budget kept - the number a player sees stays the same objective for
/// the whole raid. Single-objective quests keep the plain name (no noise).
internal static class ObjectiveLabeler
{
    /// Must match the per-marker cap Plugin.BuildFrame enforces on "l". Kept here
    /// so the label builder, not the caller, guarantees the index survives.
    public const int MaxLabelChars = 120;

    /// The labels for `count` world targets of one quest, in enumeration order.
    /// The NAME is clamped (never the "(i/n)" index), so a long quest name can
    /// not push the index past the wire budget.
    public static IList<string> Labels(string label, int count)
    {
        if (count < 1) return new string[0];
        string widest = " (" + count.ToString() + "/" + count.ToString() + ")";
        string baseLabel = Clamp(string.IsNullOrEmpty(label) ? "Quest" : label,
                                 System.Math.Max(1, MaxLabelChars - widest.Length));
        string[] outLabels = new string[count];
        for (int i = 0; i < count; i++)
            outLabels[i] = count > 1
                ? baseLabel + " (" + (i + 1).ToString() + "/" + count.ToString() + ")"
                : baseLabel;
        return outLabels;
    }

    /// Wire-budget guard: the label plus its index must still fit the per-marker
    /// cap Plugin.BuildFrame enforces (120 chars). Truncate the NAME, never the
    /// "(i/n)" index - losing the index is what makes the label ambiguous again.
    public static string Clamp(string label, int maxChars)
    {
        if (label == null) return "";
        if (label.Length <= maxChars) return label;
        int keep = Math.Max(1, maxChars - 1);
        return label.Substring(0, keep) + "\u2026";
    }
}
