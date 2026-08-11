using System.Globalization;
using System.Text.Json.Nodes;
using System.Xml.Linq;

namespace FlightDeckSimAgent;

internal sealed record FlightPlanWaypoint(string Name, double Lat, double Lon);
internal sealed record FlightPlanInfo(
    IReadOnlyList<FlightPlanWaypoint> Waypoints,
    string Departure,
    string Destination,
    int RunwayNumber,
    int RunwayDesignator,
    string PatternEntry,
    double PatternDistanceNm,
    double PatternAltitudeFt);

internal static class FlightPlan
{
    public static IReadOnlyList<FlightPlanWaypoint> Load(string path)
        => LoadInfo(path).Waypoints;

    public static FlightPlanInfo LoadInfo(string path)
    {
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path)) return EmptyInfo();

        try
        {
            var document = XDocument.Load(path);
            var root = document.Descendants("FlightPlan.FlightPlan").FirstOrDefault();
            var waypoints = document.Descendants("ATCWaypoint")
                .Select(ParseWaypoint)
                .Where(waypoint => waypoint is not null)
                .Cast<FlightPlanWaypoint>()
                .ToArray();
            if (root is null) return EmptyInfo(waypoints);

            var arrival = root.Element("ArrivalDetails");
            var pattern = root.Descendants("ApproachVisualPattern").FirstOrDefault();
            return new FlightPlanInfo(
                waypoints,
                Text(root, "DepartureID"),
                Text(root, "DestinationID"),
                Int(arrival, "RunwayNumberFP"),
                Designator(Text(arrival, "RunwayDesignatorFP")),
                Text(pattern, "ApproachVisualPatternType"),
                Number(pattern, "VisualPatternDistance", 1.5),
                Number(pattern, "VisualPatternAltitude", 0));
        }
        catch (IOException) { return EmptyInfo(); }
        catch (UnauthorizedAccessException) { return EmptyInfo(); }
        catch (System.Xml.XmlException) { return EmptyInfo(); }
    }

    public static IReadOnlyList<FlightPlanWaypoint> BuildVisualPattern(
        FlightPlanInfo plan, RunwayFacilityRaw runway)
    {
        var primary = runway.PrimaryNumber == plan.RunwayNumber &&
                      runway.PrimaryDesignator == plan.RunwayDesignator;
        var secondary = runway.SecondaryNumber == plan.RunwayNumber &&
                        runway.SecondaryDesignator == plan.RunwayDesignator;
        if (!primary && !secondary) return [];

        var heading = NormalizeHeading(runway.Heading + (secondary ? 180 : 0));
        var halfLengthNm = runway.Length / 3704.0;
        var threshold = Offset(runway.Latitude, runway.Longitude, heading + 180, halfLengthNm);
        var spacing = Math.Clamp(plan.PatternDistanceNm, 0.5, 5.0);
        var left = heading - 90;
        var reciprocal = heading + 180;
        var downwind = Offset(Offset(threshold.Lat, threshold.Lon, heading, spacing), left, spacing);
        var basePoint = Offset(Offset(threshold.Lat, threshold.Lon, reciprocal, spacing), left, spacing);
        var final = Offset(threshold.Lat, threshold.Lon, reciprocal, spacing);
        var runwayName = $"{plan.RunwayNumber:00}{DesignatorLetter(plan.RunwayDesignator)}";

        return
        [
            new($"{plan.Destination} DW", downwind.Lat, downwind.Lon),
            new($"{plan.Destination} BASE", basePoint.Lat, basePoint.Lon),
            new($"{plan.Destination} FINAL", final.Lat, final.Lon),
            new($"{plan.Destination} {runwayName}", threshold.Lat, threshold.Lon),
        ];
    }

    public static JsonArray Json(IReadOnlyList<FlightPlanWaypoint> waypoints) => new(
        waypoints.Select((waypoint, index) => (JsonNode)new JsonObject
        {
            ["id"] = waypoint.Name,
            ["lat"] = Math.Round(waypoint.Lat, 5),
            ["lon"] = Math.Round(waypoint.Lon, 5),
            ["i"] = index,
        }).ToArray());

    private static FlightPlanWaypoint? ParseWaypoint(XElement element)
    {
        var position = element.Element("WorldPosition")?.Value;
        if (string.IsNullOrWhiteSpace(position)) return null;

        var parts = position.Split(',', StringSplitOptions.TrimEntries);
        if (parts.Length < 2 || !TryCoordinate(parts[0], out var lat) ||
            !TryCoordinate(parts[1], out var lon)) return null;

        var name = element.Attribute("id")?.Value?.Trim();
        if (string.IsNullOrWhiteSpace(name))
            name = element.Descendants("ICAOIdent").FirstOrDefault()?.Value?.Trim();
        if (string.IsNullOrWhiteSpace(name)) name = "WP";
        return new FlightPlanWaypoint(name.ToUpperInvariant(), lat, lon);
    }

    private static FlightPlanInfo EmptyInfo(IReadOnlyList<FlightPlanWaypoint>? waypoints = null) =>
        new(waypoints ?? [], "", "", 0, 0, "", 1.5, 0);

    private static string Text(XElement? parent, string name) =>
        parent?.Element(name)?.Value.Trim().ToUpperInvariant() ?? "";

    private static int Int(XElement? parent, string name) =>
        int.TryParse(Text(parent, name), NumberStyles.Integer, CultureInfo.InvariantCulture,
            out var value) ? value : 0;

    private static double Number(XElement? parent, string name, double fallback) =>
        double.TryParse(Text(parent, name), NumberStyles.Float, CultureInfo.InvariantCulture,
            out var value) ? value : fallback;

    private static int Designator(string value) => value switch
    {
        "LEFT" or "L" => 1,
        "RIGHT" or "R" => 2,
        "CENTER" or "C" => 3,
        "WATER" or "W" => 4,
        "A" => 5,
        "B" => 6,
        _ => 0,
    };

    private static string DesignatorLetter(int value) => value switch
    {
        1 => "L", 2 => "R", 3 => "C", 4 => "W", 5 => "A", 6 => "B", _ => "",
    };

    private static double NormalizeHeading(double heading) => (heading % 360 + 360) % 360;

    private static (double Lat, double Lon) Offset(
        (double Lat, double Lon) point, double bearing, double distanceNm) =>
        Offset(point.Lat, point.Lon, bearing, distanceNm);

    private static (double Lat, double Lon) Offset(
        double lat, double lon, double bearing, double distanceNm)
    {
        var radians = bearing * Math.PI / 180.0;
        return (
            lat + distanceNm * Math.Cos(radians) / 60.0,
            lon + distanceNm * Math.Sin(radians) /
                (60.0 * Math.Cos(lat * Math.PI / 180.0)));
    }

    internal static bool TryCoordinate(string value, out double coordinate)
    {
        coordinate = 0;
        value = value.Trim();
        if (double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture,
                            out coordinate)) return true;
        if (value.Length < 2) return false;

        var sign = char.ToUpperInvariant(value[0]) is 'S' or 'W' ? -1.0 : 1.0;
        if (char.ToUpperInvariant(value[0]) is not ('N' or 'S' or 'E' or 'W')) return false;
        var numbers = value[1..].Split(new[] { '°', '\'', '"', ' ' },
            StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        if (numbers.Length < 1 || !double.TryParse(numbers[0], NumberStyles.Float,
            CultureInfo.InvariantCulture, out var degrees)) return false;
        var minutes = numbers.Length > 1 && double.TryParse(numbers[1], NumberStyles.Float,
            CultureInfo.InvariantCulture, out var parsedMinutes) ? parsedMinutes : 0;
        var seconds = numbers.Length > 2 && double.TryParse(numbers[2], NumberStyles.Float,
            CultureInfo.InvariantCulture, out var parsedSeconds) ? parsedSeconds : 0;
        coordinate = sign * (degrees + minutes / 60.0 + seconds / 3600.0);
        return true;
    }
}