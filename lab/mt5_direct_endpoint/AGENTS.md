# Scope
Work only inside lab/mt5_direct_endpoint unless the user explicitly says otherwise.

# Safety
Never start MT5 or MetaEditor.
Never use credentials or real endpoints.
Never enable network, firewall, WFP, bootstrap, actual launch or registry promotion.
Never modify production files.

# Efficiency
Do not scan the whole repository.
Read only files explicitly needed for the current task.
Run targeted tests first and the complete offline suite only once at the end.
Use quiet test output and inspect full logs only after a failure.
Do not generate ZIP files or long reports.
Final response: maximum 12 lines.

# Tests
python3 -m unittest discover -s lab/mt5_direct_endpoint/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/mql5/tests -q
python3 -m unittest discover -s lab/mt5_direct_endpoint/windows/tests -p 'test_*.py' -q
python3 -m compileall -q lab/mt5_direct_endpoint
