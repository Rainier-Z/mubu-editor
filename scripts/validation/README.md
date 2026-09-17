# Live write acceptance check

`live_write_e2e.py` is the permanent release acceptance gate for real Mubu
document writes. It creates one uniquely named temporary document, verifies
each mutation by reading the document back, and permanently purges only the
document ID returned by that create call.

This is an account-changing live check. Obtain the user's explicit
authorization before running it. The `--yes` flag is the command-line entry
guard; it does not replace user authorization. The Python entry point also
requires `confirmed=True` before it constructs a client or creates a document.

The acceptance sequence uses the client's public write APIs and full-definition
sync API:

1. Append three top-level nodes, including a nested child and grandchild.
2. Reorder the same siblings `ABC → CAB → ABC`, with readback after each sync.
3. Update the first node's text and insert two uniquely locatable children beneath it.
4. Exercise `taskStatus` transitions `0 → 1 → 2 → 0` and read back each value.
   Every readback must have `finish=False`, `deadline=0`, and `remindAt=0`;
   an omitted field is accepted only when its known default has that value.
5. Write an emoji and verify it by reading the node back.
6. Delete the second inserted child through the public `delete_node` path and
   read back that its stable ID is absent.
7. Move the remaining inserted child across parents and verify both parent outlines.
8. Delete the second top-level node and verify the resulting tree.

The generated temporary name is included in every post-confirmation log line.
If create returns no usable ID, the script reports that name for manual account
lookup and stops; it never attempts deletion by name. If a known-ID document
is created, cleanup runs even after a later failure. A cleanup failure reports
the returned document ID and makes the process exit nonzero. Output contains
step results and exception types, never exception messages, tokens, or
passwords.

Run from the repository root only after explicit user authorization:

```powershell
python scripts/validation/live_write_e2e.py --yes
python scripts/validation/live_write_e2e.py --yes --folder <folder-id>
```

`--folder` selects the destination folder and defaults to the account root
(`0`). These commands perform real create, write, delete, and permanent purge
operations.

Run the fake-client acceptance tests without contacting Mubu:

```powershell
python tests/test_live_write_e2e.py
```

The fake client records every public API call, including document IDs, paths,
fields, and full-definition sync targets, and the tests assert their order and
stable node IDs. Sync assertions cover acceptance orchestration and tree
identity; changeset payload correctness remains covered by the core client
tests.
