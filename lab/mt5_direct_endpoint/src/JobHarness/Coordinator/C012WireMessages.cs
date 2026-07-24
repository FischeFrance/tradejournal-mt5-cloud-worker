using System.Text.Json;
using System.Text.Json.Serialization;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Wire schema version 1 is frozen. A future change bumps this constant; a mismatch is
// rejected as invalid rather than negotiated.
public static class C012WireSchema
{
    public const int Version = 1;
}

public sealed record C012WireRequest(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("c012_session_id")] Guid SessionId,
    [property: JsonPropertyName("sequence_number")] long SequenceNumber,
    [property: JsonPropertyName("control")] C012Control Control,
    [property: JsonPropertyName("request_type")] C012RequestType RequestType,
    [property: JsonPropertyName("hmac_sha256")] string HmacSha256Hex);

public sealed record C012WireResponse(
    [property: JsonPropertyName("schema_version")] int SchemaVersion,
    [property: JsonPropertyName("c012_session_id")] Guid SessionId,
    [property: JsonPropertyName("sequence_number")] long SequenceNumber,
    [property: JsonPropertyName("accepted")] bool Accepted,
    [property: JsonPropertyName("resulting_state")] C012State ResultingState,
    [property: JsonPropertyName("reason")] C012RejectionReason Reason,
    [property: JsonPropertyName("hmac_sha256")] string HmacSha256Hex);

// Shared (de)serialization options for both peers: enums as their member-name strings
// (matching C012CanonicalPayload's use of Enum.ToString()), compact output, and unknown
// fields rejected outright rather than silently ignored.
public static class C012WireJsonOptions
{
    public static readonly JsonSerializerOptions Instance = Build();

    private static JsonSerializerOptions Build()
    {
        var options = new JsonSerializerOptions
        {
            WriteIndented = false,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        };
        options.Converters.Add(new JsonStringEnumConverter());
        return options;
    }
}

// Structural validity independent of JSON deserialization mechanics: checked before a
// message is trusted enough to reach HMAC verification.
public static class C012WireValidation
{
    private const int HmacHexLength = 64;

    public static bool IsValid(C012WireRequest request) =>
        request.SchemaVersion == C012WireSchema.Version
        && Enum.IsDefined(request.Control)
        && Enum.IsDefined(request.RequestType)
        && request.SequenceNumber >= 1
        && IsLowerHex(request.HmacSha256Hex);

    public static bool IsValid(C012WireResponse response) =>
        response.SchemaVersion == C012WireSchema.Version
        && Enum.IsDefined(response.ResultingState)
        && Enum.IsDefined(response.Reason)
        && response.SequenceNumber >= 1
        && IsLowerHex(response.HmacSha256Hex);

    private static bool IsLowerHex(string value)
    {
        if (value.Length != HmacHexLength)
        {
            return false;
        }

        foreach (char c in value)
        {
            bool isLowerHexDigit = c is (>= '0' and <= '9') or (>= 'a' and <= 'f');
            if (!isLowerHexDigit)
            {
                return false;
            }
        }

        return true;
    }
}
