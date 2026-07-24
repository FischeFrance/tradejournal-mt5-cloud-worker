using System.Text.Json;

namespace TradeJournal.Lab.JobHarness.Coordinator;

public enum C012ChannelOutcome
{
    RequestProcessed,
    PeerDisconnected,
    Malformed,
    AuthenticationFailed,
}

// Server side of one accepted IPC connection. Every incoming frame is framed, structurally
// validated, and HMAC-verified before it ever reaches the wrapped C012RequestSequencer; a
// message that fails any of those checks is refused silently (no response frame is sent)
// and never mutates the sequencer's state. A message that reaches the sequencer and is
// rejected there gets a normal, signed rejection response -- that is ordinary protocol
// operation, not a security event.
public sealed class C012ServerChannel
{
    private readonly Stream _stream;
    private readonly C012SessionSecret _secret;
    private readonly C012RequestSequencer _sequencer;

    public C012ServerChannel(Stream stream, C012SessionSecret secret, C012RequestSequencer sequencer)
    {
        ArgumentNullException.ThrowIfNull(stream);
        ArgumentNullException.ThrowIfNull(secret);
        ArgumentNullException.ThrowIfNull(sequencer);
        _stream = stream;
        _secret = secret;
        _sequencer = sequencer;
    }

    public async Task<C012ChannelOutcome> ProcessNextRequestAsync(CancellationToken cancellationToken)
    {
        byte[]? frame;
        try
        {
            frame = await C012FrameCodec.ReadFrameAsync(_stream, cancellationToken).ConfigureAwait(false);
        }
        catch (C012FramingException)
        {
            return C012ChannelOutcome.Malformed;
        }
        catch (IOException)
        {
            return C012ChannelOutcome.PeerDisconnected;
        }

        if (frame is null)
        {
            return C012ChannelOutcome.PeerDisconnected;
        }

        C012WireRequest? request = TryParseRequest(frame);
        if (request is null)
        {
            return C012ChannelOutcome.Malformed;
        }

        if (!C012MessageAuthenticator.VerifyRequest(_secret.Value, request))
        {
            return C012ChannelOutcome.AuthenticationFailed;
        }

        var envelope = new C012RequestEnvelope(request.SessionId, request.SequenceNumber, request.Control, request.RequestType);
        C012TransitionResult result = _sequencer.Apply(envelope);
        C012WireResponse response = BuildSignedResponse(request, result);

        try
        {
            byte[] responseBytes = JsonSerializer.SerializeToUtf8Bytes(response, C012WireJsonOptions.Instance);
            await C012FrameCodec.WriteFrameAsync(_stream, responseBytes, cancellationToken).ConfigureAwait(false);
        }
        catch (IOException)
        {
            return C012ChannelOutcome.PeerDisconnected;
        }

        return C012ChannelOutcome.RequestProcessed;
    }

    private static C012WireRequest? TryParseRequest(byte[] frame)
    {
        try
        {
            C012WireRequest? request = JsonSerializer.Deserialize<C012WireRequest>(frame, C012WireJsonOptions.Instance);
            return request is not null && C012WireValidation.IsValid(request) ? request : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private C012WireResponse BuildSignedResponse(C012WireRequest request, C012TransitionResult result)
    {
        string hmac = C012MessageAuthenticator.SignResponse(
            _secret.Value, C012WireSchema.Version, request.SessionId, request.SequenceNumber,
            result.Accepted, result.ResultingState, result.Reason);
        return new C012WireResponse(
            C012WireSchema.Version, request.SessionId, request.SequenceNumber,
            result.Accepted, result.ResultingState, result.Reason, hmac);
    }
}
