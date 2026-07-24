using System.Buffers;
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Canonical, byte-exact payload construction used for HMAC signing and verification.
// C012ServerChannel and C012ClientChannel both call these same two methods, so the two
// sides can never independently drift in field order, naming, or formatting. The
// hmac_sha256 field itself is never part of this payload -- it does not exist yet when a
// request is signed, and is stripped by construction (never read) when a received message
// is re-signed for comparison.
public static class C012CanonicalPayload
{
    private const string DomainTag = "MT5_LAB_C012_IPC";
    public const string RequestMessageType = "REQUEST";
    public const string ResponseMessageType = "RESPONSE";

    public static byte[] ForRequest(
        int schemaVersion, Guid sessionId, long sequenceNumber, C012Control control, C012RequestType requestType)
    {
        var buffer = new ArrayBufferWriter<byte>();
        using (var writer = new Utf8JsonWriter(buffer, new JsonWriterOptions { Indented = false }))
        {
            writer.WriteStartObject();
            writer.WriteNumber("schema_version", schemaVersion);
            writer.WriteString("c012_session_id", sessionId);
            writer.WriteNumber("sequence_number", sequenceNumber);
            writer.WriteString("control", control.ToString());
            writer.WriteString("request_type", requestType.ToString());
            writer.WriteEndObject();
        }

        return Prefix(RequestMessageType, schemaVersion, buffer.WrittenSpan);
    }

    public static byte[] ForResponse(
        int schemaVersion, Guid sessionId, long sequenceNumber, bool accepted,
        C012State resultingState, C012RejectionReason reason)
    {
        var buffer = new ArrayBufferWriter<byte>();
        using (var writer = new Utf8JsonWriter(buffer, new JsonWriterOptions { Indented = false }))
        {
            writer.WriteStartObject();
            writer.WriteNumber("schema_version", schemaVersion);
            writer.WriteString("c012_session_id", sessionId);
            writer.WriteNumber("sequence_number", sequenceNumber);
            writer.WriteBoolean("accepted", accepted);
            writer.WriteString("resulting_state", resultingState.ToString());
            writer.WriteString("reason", reason.ToString());
            writer.WriteEndObject();
        }

        return Prefix(ResponseMessageType, schemaVersion, buffer.WrittenSpan);
    }

    private static byte[] Prefix(string messageType, int schemaVersion, ReadOnlySpan<byte> canonicalJson)
    {
        string prefixText = DomainTag + "\0" + messageType + "\0" +
            schemaVersion.ToString(CultureInfo.InvariantCulture) + "\0";
        byte[] head = Encoding.UTF8.GetBytes(prefixText);
        byte[] result = new byte[head.Length + canonicalJson.Length];
        head.CopyTo(result.AsSpan());
        canonicalJson.CopyTo(result.AsSpan(head.Length));
        return result;
    }
}
