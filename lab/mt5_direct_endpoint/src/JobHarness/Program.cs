using TradeJournal.Lab.JobHarness;
using TradeJournal.Lab.JobHarness.Coordinator;

if (args is ["c012-host", "start", .. var hostArgs])
{
    return C012HostCli.Run(hostArgs, Console.Out, Console.Error);
}

if (args is ["c012-client", var clientVerb, .. var clientArgs])
{
    return C012ClientCli.Run(clientVerb, clientArgs, Console.Out, Console.Error);
}

return HarnessApplication.Execute(args, Console.Out, Console.Error);
