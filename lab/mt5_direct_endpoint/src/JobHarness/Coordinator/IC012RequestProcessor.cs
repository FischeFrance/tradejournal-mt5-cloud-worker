namespace TradeJournal.Lab.JobHarness.Coordinator;

// Whatever ultimately decides how an authenticated, in-order request maps to a state
// transition: a bare C012RequestSequencer for B2/B3-level testing, or a
// C012OrchestratingProcessor that performs real Job Object/process side effects before
// letting the FSM record success. C012ServerChannel depends only on this interface.
public interface IC012RequestProcessor
{
    C012TransitionResult Apply(C012RequestEnvelope request);
}
