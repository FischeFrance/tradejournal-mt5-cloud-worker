using TradeJournal.Lab.JobHarness;
using TradeJournal.Lab.JobHarness.Coordinator;

// Self-invocation targets for `c012-host start-innocuous` only (see C012HostCli.RunInnocuous):
// that verb re-invokes this very executable as its own root/submitter, so this process must
// understand these two flags and behave innocuously (long-lived vs. immediate exit) instead of
// running the normal CLI dispatch below. Must precede every other top-level statement in this
// file -- C# requires every top-level statement to precede any type declaration, and these
// checks must in turn run before the pattern-match dispatch below. Never reachable except as a
// Job-Object-launched child of `start-innocuous`; `c012-host start` and every other command
// never pass these flags to anything.
if (args is ["--innocent-lab-sleeper"])
{
    System.Threading.Thread.Sleep(TimeSpan.FromSeconds(30));
    return 0;
}

if (args is ["--innocent-lab-exit-zero"])
{
    return 0;
}

if (args is ["c012-host", "start", .. var hostArgs])
{
    return C012HostCli.Run(hostArgs, Console.Out, Console.Error);
}

if (args is ["c012-host", "start-innocuous", .. var innocuousArgs])
{
    return C012HostCli.RunInnocuous(innocuousArgs, Console.Out, Console.Error);
}

if (args is ["c012-client", var clientVerb, .. var clientArgs])
{
    return C012ClientCli.Run(clientVerb, clientArgs, Console.Out, Console.Error);
}

return HarnessApplication.Execute(args, Console.Out, Console.Error);
