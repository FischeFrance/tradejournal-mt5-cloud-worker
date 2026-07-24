using System.Text.Json;

namespace TradeJournal.Lab.JobHarness.Coordinator;

// Client side of one IPC connection. Tracks its own next sequence number locally, only
// advancing it once the server confirms acceptance -- a rejected request leaves the same
// slot open for a corrected retry, matching C012RequestSequencer's own accept-only
// advancement rule so client and server expectations never diverge. The starting sequence
// number is always supplied by the caller (never hardcoded to 1): a fresh instance is
// constructed for every separate c012-client process, so only an externally persisted
// cursor (C012SessionSequenceCursor) lets the caller know the session's real, current
// sequence number.
public sealed class C012ClientChannel
{
    private readonly Stream _stream;
    private readonly C012SessionSecret _secret;
    private readonly Guid _sessionId;
    private long _nextSequence;

    public C012ClientChannel(Stream stream, C012SessionSecret secret, Guid sessionId, long initialSequence)
    {
        ArgumentNullException.ThrowIfNull(stream);
        ArgumentNullException.ThrowIfNull(secret);
        ArgumentOutOfRangeException.ThrowIfLessThan(initialSequence, 1);
        _stream = stream;
        _secret = secret;
        _sessionId = sessionId;
        _nextSequence = initialSequence;
    }

    public async Task<C012TransitionResult> SendAsync(
        C012Control control, C012RequestType requestType, CancellationToken cancellationToken)
    {
        long sequenceNumber = _nextSequence;
        string requestHmac = C012MessageAuthenticator.SignRequest(
            _secret.Value, C012WireSchema.Version, _sessionId, sequenceNumber, control, requestType);
        var request = new C012WireRequest(
            C012WireSchema.Version, _sessionId, sequenceNumber, control, requestType, requestHmac);

        byte[] requestBytes = JsonSerializer.SerializeToUtf8Bytes(request, C012WireJsonOptions.Instance);
        await C012FrameCodec.WriteFrameAsync(_stream, requestBytes, cancellationToken).ConfigureAwait(false);

        byte[]? responseFrame = await C012FrameCodec.ReadFrameAsync(_stream, cancellationToken).ConfigureAwait(false);
        if (responseFrame is null)
        {
            throw new C012FramingException("The server closed the connection before responding.");
        }

        C012WireResponse? response;
        try
        {
            response = JsonSerializer.Deserialize<C012WireResponse>(responseFrame, C012WireJsonOptions.Instance);
        }
        catch (JsonException exception)
        {
            throw new C012FramingException("The server response could not be parsed.", exception);
        }

        if (response is null || !C012WireValidation.IsValid(response))
        {
            throw new C012FramingException("The server response is malformed.");
        }

        if (response.SessionId != _sessionId || response.SequenceNumber != sequenceNumber)
        {
            throw new C012FramingException("The server response does not match the outstanding request.");
        }

        if (!C012MessageAuthenticator.VerifyResponse(_secret.Value, response))
        {
            throw new C012FramingException("The server response failed authentication.");
        }

        if (response.Accepted)
        {
            _nextSequence++;
        }

        return new C012TransitionResult(response.Accepted, response.ResultingState, response.Reason);
    }
}
