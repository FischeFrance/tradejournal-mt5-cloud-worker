using System.Security.Cryptography;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// HMAC-SHA256 signing and verification over C012CanonicalPayload. Verification always uses
// a constant-time comparison (CryptographicOperations.FixedTimeEquals), the same primitive
// TargetExecutableLease.MatchesExpectedSha256 already uses for the executable digest check.
public static class C012MessageAuthenticator
{
    public static string SignRequest(
        ReadOnlySpan<byte> secret, int schemaVersion, Guid sessionId, long sequenceNumber,
        C012Control control, C012RequestType requestType)
    {
        byte[] payload = C012CanonicalPayload.ForRequest(schemaVersion, sessionId, sequenceNumber, control, requestType);
        return ToHex(HMACSHA256.HashData(secret, payload));
    }

    public static string SignResponse(
        ReadOnlySpan<byte> secret, int schemaVersion, Guid sessionId, long sequenceNumber,
        bool accepted, C012State resultingState, C012RejectionReason reason)
    {
        byte[] payload = C012CanonicalPayload.ForResponse(
            schemaVersion, sessionId, sequenceNumber, accepted, resultingState, reason);
        return ToHex(HMACSHA256.HashData(secret, payload));
    }

    public static bool VerifyRequest(ReadOnlySpan<byte> secret, C012WireRequest request)
    {
        ArgumentNullException.ThrowIfNull(request);
        byte[] payload = C012CanonicalPayload.ForRequest(
            request.SchemaVersion, request.SessionId, request.SequenceNumber, request.Control, request.RequestType);
        return Matches(secret, payload, request.HmacSha256Hex);
    }

    public static bool VerifyResponse(ReadOnlySpan<byte> secret, C012WireResponse response)
    {
        ArgumentNullException.ThrowIfNull(response);
        byte[] payload = C012CanonicalPayload.ForResponse(
            response.SchemaVersion, response.SessionId, response.SequenceNumber,
            response.Accepted, response.ResultingState, response.Reason);
        return Matches(secret, payload, response.HmacSha256Hex);
    }

    private static bool Matches(ReadOnlySpan<byte> secret, byte[] payload, string claimedHmacHex)
    {
        if (!TryDecodeHex(claimedHmacHex, out byte[] actual))
        {
            return false;
        }

        byte[] expected = HMACSHA256.HashData(secret, payload);
        return expected.Length == actual.Length && CryptographicOperations.FixedTimeEquals(expected, actual);
    }

    private static bool TryDecodeHex(string hex, out byte[] bytes)
    {
        try
        {
            bytes = Convert.FromHexString(hex);
            return true;
        }
        catch (FormatException)
        {
            bytes = [];
            return false;
        }
    }

    private static string ToHex(byte[] bytes) => Convert.ToHexString(bytes).ToLowerInvariant();
}
