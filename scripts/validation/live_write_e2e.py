"""Permanent live acceptance gate for Mubu document writes."""

import argparse
import json
import secrets
import struct
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from mubu.client import MubuClient  # noqa: E402
from mubu.config import MubuError  # noqa: E402
from mubu.convert import (  # noqa: E402
    formula_html,
    highlight_html,
    mask_html,
    mention_html,
    node_mention_html,
)

DOC_NAME_PREFIX = "live-write-e2e-"
TOP_A = "E2E top A"
TOP_A_UPDATED = "E2E top A updated"
TOP_B = "E2E top B"
TOP_C = "E2E top C"
NESTED_CHILD = "E2E nested child"
NESTED_GRANDCHILD = "E2E nested grandchild"
INSERTED_CHILD = "E2E inserted child"
DELETABLE_CHILD = "E2E deletable child"
EMOJI = "🧪"
MASK_HTML = mask_html("E2E masked text")
HIGHLIGHT_HTML = highlight_html("E2E highlighted text")
SUMMARY_DIAGNOSTIC_LIMIT = 3000
FORMULA_HTML = formula_html("x^2")
MENTION_HTML = mention_html("target-doc", "E2E target document")
NODE_MENTION_HTML = node_mention_html(
    "target-doc", "target-node", "E2E target node")


class _StepFailed(Exception):
    """Marks a reported acceptance-step failure without exposing API details."""


def _document_name():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{DOC_NAME_PREFIX}{timestamp}-{secrets.token_hex(6)}"


def _text_shape(nodes):
    if not isinstance(nodes, list):
        raise AssertionError("readback nodes are not a list")
    shape = []
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("text"), str):
            raise AssertionError("readback contains a node without text")
        children = node.get("children", [])
        if children is None:
            children = []
        if not isinstance(children, list):
            raise AssertionError("readback children are not a list")
        shape.append((node["text"], tuple(_text_shape(children))))
    return shape


def _assert_shape(document, expected):
    if not isinstance(document, dict):
        raise AssertionError("readback document is not an object")
    actual = tuple(_text_shape(document.get("nodes")))
    if actual != expected:
        raise AssertionError("readback structure did not match the expected outline")


def _node_at(document, path):
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), list):
        raise AssertionError("readback document has no nodes list")
    node = document["nodes"][path[1]]
    for offset in range(2, len(path), 2):
        if path[offset] != "children":
            raise AssertionError("unsupported readback path")
        node = node["children"][path[offset + 1]]
    return node


def _assert_field(client, doc_id, path, field, expected):
    node = _node_at(client.get_doc(doc_id), path)
    if node.get(field) != expected:
        raise AssertionError(f"readback field {field} did not match")


def _assert_task_contract(document, path, status):
    """Check status and the task fields that status changes must reset.

    Some server responses omit fields whose values are their protocol defaults;
    an omitted default is therefore equivalent to an explicit default here.
    Any returned non-default value remains a failure.
    """
    node = _node_at(document, path)
    if node.get("taskStatus", 0) != status:
        raise AssertionError("readback field taskStatus did not match")
    for field, default in (("finish", False), ("deadline", 0), ("remindAt", 0)):
        if node.get(field, default) != default:
            raise AssertionError(f"readback field {field} did not match")


def _path_for_node_id(document, node_id):
    def visit(nodes, prefix):
        for index, node in enumerate(nodes):
            path = prefix + ["nodes", index] if prefix == [] else prefix + ["children", index]
            if node.get("id") == node_id:
                return path
            child_path = visit(node.get("children") or [], path)
            if child_path is not None:
                return child_path
        return None

    path = visit(document.get("nodes") or [], [])
    if path is None:
        raise AssertionError("created node ID was not found in readback")
    return path


def _assert_id_absent(document, node_id):
    def visit(nodes):
        for node in nodes:
            if node.get("id") == node_id or visit(node.get("children") or []):
                return True
        return False

    if visit(document.get("nodes") or []):
        raise AssertionError("deleted node ID remained in readback")


def _assert_summary_consistent(document, paths, summary_id):
    """Require one identical summary replica on every requested member node."""
    members = [_node_at(document, path) for path in paths]
    expected_member_ids = [node.get("id") for node in members]
    records = []
    for node in members:
        matches = [item for item in node.get("summaryData", [])
                   if item.get("id") == summary_id]
        if len(matches) != 1:
            raise AssertionError("summary replica was not present exactly once")
        records.append(matches[0])
    if any(record != records[0] for record in records[1:]):
        raise AssertionError("summary replicas did not match")
    if records[0].get("memberIds") != expected_member_ids:
        raise AssertionError("summary replica member IDs did not match")
    if records[0].get("modified") != records[0].get("createdAt"):
        raise AssertionError("summary replica timestamps did not match")


def _assert_summary_absent(document, paths, summary_id):
    for path in paths:
        node = _node_at(document, path)
        if any(item.get("id") == summary_id
               for item in (node.get("summaryData") or [])):
            raise AssertionError("deleted summary remained in readback")


def _emit_summary_failure_diagnostic(name, client, doc_id, paths, emit):
    """Emit only selected members' summaryData after a summary write failure."""
    try:
        document = client.get_doc(doc_id)
        members = []
        for path in paths:
            node = _node_at(document, path)
            members.append({
                "path": list(path),
                "summaryData": node.get("summaryData", []),
            })
        payload = {"members": members}
        encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > SUMMARY_DIAGNOSTIC_LIMIT:
            encoded = json.dumps(
                {"members": [
                    {"path": list(path), "summaryData": "<truncated>"}
                    for path in paths
                ]},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        emit(f"[INFO] name={name} summary readback diagnostic summaryData={encoded}")
    except Exception as error:
        emit(
            f"[WARN] name={name} summary readback diagnostic unavailable "
            f"({type(error).__name__})"
        )


def _minimal_png(width=17, height=23):
    """Build a tiny valid PNG for the image acceptance check."""
    data = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(data) - 4) + data
    return (b"\x89PNG\r\n\x1a\n" + chunk
            + struct.pack(">I", zlib.crc32(data) & 0xFFFFFFFF))


def _assert_moved_node(document, expected_shape, node_id):
    _assert_shape(document, expected_shape)
    moved = _node_at(document, ["nodes", 2, "children", 0])
    if moved.get("id") != node_id:
        raise AssertionError("cross-parent move changed the node identity")


def _assert_root_promoted_node(document, expected_shape, node_id):
    _assert_shape(document, expected_shape)
    promoted = _node_at(document, ["nodes", 1])
    if promoted.get("id") != node_id:
        raise AssertionError("nested-to-root move changed the node identity")


def _assert_nested_node_restored(document, expected_shape, node_id):
    _assert_shape(document, expected_shape)
    restored = _node_at(document, ["nodes", 0, "children", 0])
    if restored.get("id") != node_id:
        raise AssertionError("root-to-nested restore changed the node identity")


def _assert_image(document, path, width, height, display_width):
    images = _node_at(document, path).get("images")
    if not isinstance(images, list) or len(images) != 1:
        raise AssertionError("readback did not contain exactly one image")
    image = images[0]
    if (image.get("ow"), image.get("oh"), image.get("w")) != (
            width, height, display_width):
        raise AssertionError("readback image dimensions did not match")


def _assert_link_line(document, source_id, target_id):
    lines = _node_at(document, ["nodes", 0]).get("linkLines")
    if not isinstance(lines, list) or len(lines) != 1:
        raise AssertionError("readback did not contain exactly one link line")
    line = lines[0]
    if (line.get("fromNodeId"), line.get("toNodeId")) != (source_id, target_id):
        raise AssertionError("readback link line endpoints did not match")
    if (line.get("fromAttachment") or {}).get("side") != "right":
        raise AssertionError("readback link line side did not match")


def _run_step(name, client, doc_id, label, write, verify, emit,
              on_mubu_error=None):
    try:
        write()
        verify()
    except Exception as error:
        if isinstance(error, MubuError) and on_mubu_error is not None:
            try:
                on_mubu_error()
            except Exception as diagnostic_error:
                emit(
                    f"[WARN] name={name} diagnostic collection failed "
                    f"({type(diagnostic_error).__name__})"
                )
        error_label = type(error).__name__
        if isinstance(error, MubuError):
            message = getattr(error, "msg", "")
            if isinstance(message, str) and message:
                message = message[:300].replace("\r", r"\r").replace("\n", r"\n")
                error_label += f": {message}"
        emit(f"[FAIL] name={name} {label} ({error_label})")
        raise _StepFailed from None
    emit(f"[PASS] name={name} {label}")


def run_live_write_validation(client=None, folder_id="0", emit=print, *,
                              confirmed=False, client_factory=None):
    """Run the verified write sequence and purge only its returned document ID.

    ``confirmed=True`` is checked before client construction and document
    creation.  A fake client may be injected for offline tests; otherwise the
    factory is called only after confirmation.
    """
    if confirmed is not True:
        emit("[FAIL] explicit confirmation is required; no client or document was created")
        return False

    name = _document_name()
    doc_id = None
    succeeded = True
    if client is None:
        try:
            client = (client_factory or MubuClient)()
        except Exception as error:
            emit(f"[FAIL] name={name} create Mubu client ({type(error).__name__})")
            return False

    try:
        try:
            returned_id = client.create_doc(name, folder_id, "")
            if not isinstance(returned_id, str) or not returned_id:
                raise ValueError("create_doc returned no document ID")
            doc_id = returned_id
        except Exception as error:
            # An unknown create result has no safe identifier.  Keep the name
            # for manual lookup and never pass it to purge.
            emit(f"[FAIL] name={name} create isolated empty document ({type(error).__name__})")
            raise _StepFailed from None

        empty = ()
        _run_step(name, client, doc_id, "create isolated empty document",
                  lambda: None,
                  lambda: _assert_shape(client.get_doc(doc_id), empty), emit)

        initial = (
            (TOP_A, ((NESTED_CHILD, ((NESTED_GRANDCHILD, ()),)),)),
            (TOP_B, ()),
            (TOP_C, ()),
        )
        _run_step(
            name, client, doc_id, "append three top-level nodes with a subtree",
            lambda: client.append_top_nodes(doc_id, [
                {"text": TOP_A, "children": [{
                    "text": NESTED_CHILD,
                    "children": [{"text": NESTED_GRANDCHILD, "children": []}],
                }]},
                {"text": TOP_B, "children": []},
                {"text": TOP_C, "children": []},
            ]),
            lambda: _assert_shape(client.get_doc(doc_id), initial), emit)

        # Exercise the public move API for same-parent reordering.
        cab = ((TOP_C, ()), (TOP_A, ((NESTED_CHILD, ((NESTED_GRANDCHILD, ()),)),)),
               (TOP_B, ()))
        _run_step(name, client, doc_id, "same-parent order ABC → CAB",
                  lambda: client.move_node(doc_id, ["nodes", 2], 0),
                  lambda: _assert_shape(client.get_doc(doc_id), cab), emit)
        _run_step(name, client, doc_id, "same-parent order CAB → ABC",
                  lambda: client.move_node(doc_id, ["nodes", 0], 2),
                  lambda: _assert_shape(client.get_doc(doc_id), initial), emit)
        emit(f"[PASS] name={name} same-parent order ABC → CAB → ABC")

        nested_node = _node_at(
            client.get_doc(doc_id), ["nodes", 0, "children", 0])
        nested_id = nested_node.get("id")
        if not isinstance(nested_id, str) or not nested_id:
            raise ValueError("nested node has no stable ID")
        promoted = (
            (TOP_A, ()),
            (NESTED_CHILD, ((NESTED_GRANDCHILD, ()),)),
            (TOP_B, ()),
            (TOP_C, ()),
        )
        _run_step(
            name, client, doc_id, "nested node to root and readback",
            lambda: client.move_node(
                doc_id, ["nodes", 0, "children", 0], 1),
            lambda: _assert_root_promoted_node(
                client.get_doc(doc_id), promoted, nested_id), emit)
        _run_step(
            name, client, doc_id, "restore nested node and readback",
            lambda: client.move_node(
                doc_id, ["nodes", 1], 0, ["nodes", 0]),
            lambda: _assert_nested_node_restored(
                client.get_doc(doc_id), initial, nested_id), emit)

        updated = (
            (TOP_A_UPDATED, ((NESTED_CHILD, ((NESTED_GRANDCHILD, ()),)),)),
            (TOP_B, ()),
            (TOP_C, ()),
        )
        _run_step(name, client, doc_id, "update top-level node text",
                  lambda: client.update_node_text(doc_id, ["nodes", 0], TOP_A_UPDATED),
                  lambda: _assert_shape(client.get_doc(doc_id), updated), emit)

        inserted = (
            (TOP_A_UPDATED, (
                (NESTED_CHILD, ((NESTED_GRANDCHILD, ()),)),
                (INSERTED_CHILD, ()),
                (DELETABLE_CHILD, ()),
            )),
            (TOP_B, ()),
            (TOP_C, ()),
        )
        inserted_ids = []

        def insert_children():
            created_ids = client.insert_child_nodes(
                doc_id, ["nodes", 0], [INSERTED_CHILD, DELETABLE_CHILD])
            if (not isinstance(created_ids, list) or len(created_ids) != 2
                    or not all(isinstance(node_id, str) and node_id for node_id in created_ids)):
                raise ValueError("insert_child_nodes returned unusable node IDs")
            inserted_ids.extend(created_ids)

        _run_step(name, client, doc_id, "insert child beneath updated node",
                  insert_children,
                  lambda: _assert_shape(client.get_doc(doc_id), inserted), emit)

        # Use the captured-safe summary shape: two sibling members under the
        # same parent, with the protocol's initial empty summary text.
        summary_paths = [
            ["nodes", 0, "children", 1],
            ["nodes", 0, "children", 2],
        ]
        summary_id_holder = []

        def create_summary():
            summary_id = client.create_summary(
                doc_id, summary_paths, text="")
            if not isinstance(summary_id, str) or not summary_id:
                raise ValueError("create_summary returned unusable summary ID")
            summary_id_holder.append(summary_id)

        _run_step(name, client, doc_id, "create multi-member summary",
                   create_summary,
                   lambda: _assert_summary_consistent(
                       client.get_doc(doc_id), summary_paths, summary_id_holder[0]), emit,
                   on_mubu_error=lambda: _emit_summary_failure_diagnostic(
                       name, client, doc_id, summary_paths, emit))
        summary_id = summary_id_holder[0]
        _run_step(name, client, doc_id, "delete multi-member summary",
                  lambda: client.delete_summary(doc_id, summary_paths, summary_id),
                  lambda: _assert_summary_absent(
                      client.get_doc(doc_id), summary_paths, summary_id), emit)

        _run_step(name, client, doc_id, "initial taskStatus 0 readback",
                  lambda: None,
                  lambda: _assert_task_contract(
                      client.get_doc(doc_id), ["nodes", 0], 0), emit)

        for status in (1, 2):
            previous = 0 if status == 1 else 1
            _run_step(
                name, client, doc_id, f"taskStatus {previous}→{status}",
                lambda status=status: client.set_node_fields(
                    doc_id, ["nodes", 0], {"taskStatus": status}),
                lambda status=status: _assert_task_contract(
                    client.get_doc(doc_id), ["nodes", 0], status), emit)

        # The node-level API has a confirmed forward protocol.  Exercise the
        # reset through the public full-definition sync API.
        reset_definition = client.get_doc(doc_id)
        reset_definition["nodes"][0]["taskStatus"] = 0
        _run_step(name, client, doc_id, "taskStatus 2→0 via sync",
                  lambda: client.sync_doc_definition(doc_id, reset_definition),
                  lambda: _assert_task_contract(
                      client.get_doc(doc_id), ["nodes", 0], 0), emit)
        emit(f"[PASS] name={name} taskStatus 0→1→2→0 readback")

        _run_step(name, client, doc_id, "emoji write/readback",
                  lambda: client.set_node_fields(
                      doc_id, ["nodes", 0], {"emoji": EMOJI}),
                  lambda: _assert_field(
                      client, doc_id, ["nodes", 0], "emoji", EMOJI), emit)

        for collapsed, label in ((True, "collapse"), (False, "expand")):
            _run_step(name, client, doc_id, f"{label} node",
                      lambda collapsed=collapsed: client.set_node_fields(
                          doc_id, ["nodes", 0], {"collapsed": collapsed}),
                      lambda collapsed=collapsed: _assert_field(
                          client, doc_id, ["nodes", 0], "collapsed", collapsed), emit)

        _run_step(name, client, doc_id, "formula text write/readback",
                  lambda: client.set_node_fields(
                      doc_id, ["nodes", 0], {"text": FORMULA_HTML}),
                  lambda: _assert_field(
                      client, doc_id, ["nodes", 0], "text", FORMULA_HTML), emit)

        _run_step(name, client, doc_id, "document mention text write/readback",
                  lambda: client.set_node_fields(
                      doc_id, ["nodes", 0], {"text": MENTION_HTML}),
                  lambda: _assert_field(
                      client, doc_id, ["nodes", 0], "text", MENTION_HTML), emit)

        _run_step(name, client, doc_id, "node mention text write/readback",
                  lambda: client.set_node_fields(
                      doc_id, ["nodes", 0], {"text": NODE_MENTION_HTML}),
                  lambda: _assert_field(
                      client, doc_id, ["nodes", 0], "text", NODE_MENTION_HTML), emit)

        _run_step(name, client, doc_id, "mask text write/readback",
                  lambda: client.set_node_fields(
                      doc_id, ["nodes", 0], {"text": MASK_HTML}),
                  lambda: _assert_field(
                      client, doc_id, ["nodes", 0], "text", MASK_HTML), emit)

        _run_step(name, client, doc_id, "highlight text write/readback",
                  lambda: client.set_node_fields(
                      doc_id, ["nodes", 0], {"text": HIGHLIGHT_HTML}),
                  lambda: _assert_field(
                      client, doc_id, ["nodes", 0], "text", HIGHLIGHT_HTML), emit)

        delete_snapshot = client.get_doc(doc_id)
        deletable_id = inserted_ids[1]
        deletable_path = _path_for_node_id(delete_snapshot, deletable_id)
        _run_step(name, client, doc_id, "delete nested inserted child",
                  lambda: client.delete_node(doc_id, deletable_path),
                  lambda: _assert_id_absent(client.get_doc(doc_id), deletable_id), emit)

        # Move the inserted child from A to C through the public move API.
        after_move = (
            (HIGHLIGHT_HTML, ((NESTED_CHILD, ((NESTED_GRANDCHILD, ()),)),)),
            (TOP_B, ()),
            (TOP_C, ((INSERTED_CHILD, ()),)),
        )
        moved_id = inserted_ids[0]
        moved_path = _path_for_node_id(client.get_doc(doc_id), moved_id)
        _run_step(name, client, doc_id, "cross-parent move and readback",
                  lambda: client.move_node(doc_id, moved_path, 0, ["nodes", 2]),
                  lambda: _assert_moved_node(
                      client.get_doc(doc_id), after_move, moved_id), emit)

        with tempfile.TemporaryDirectory(prefix="mubu-live-write-") as temp_dir:
            image_path = Path(temp_dir) / "e2e.png"
            image_path.write_bytes(_minimal_png())
            _run_step(name, client, doc_id, "image write/readback",
                      lambda: client.attach_image(
                          doc_id, ["nodes", 0], image_path, width=240),
                      lambda: _assert_image(
                          client.get_doc(doc_id), ["nodes", 0], 17, 23, 240), emit)

        source_node = _node_at(client.get_doc(doc_id), ["nodes", 0])
        target_node = _node_at(client.get_doc(doc_id), ["nodes", 2])
        _run_step(name, client, doc_id, "link nodes and readback",
                  lambda: client.link_nodes(
                      doc_id, ["nodes", 0], ["nodes", 2], side="right"),
                  lambda: _assert_link_line(
                      client.get_doc(doc_id), source_node["id"], target_node["id"]), emit)

        after_delete = (after_move[0], after_move[2])
        _run_step(name, client, doc_id, "delete one top-level node",
                  lambda: client.delete_top_nodes(doc_id, [1]),
                  lambda: _assert_shape(client.get_doc(doc_id), after_delete), emit)
    except _StepFailed:
        succeeded = False
    except Exception as error:
        emit(f"[FAIL] name={name} acceptance sequence ({type(error).__name__})")
        succeeded = False
    finally:
        if doc_id:
            try:
                client.purge_item(doc_id, item_type="doc")
            except Exception as error:
                emit(f"[FAIL] name={name} cleanup doc_id={doc_id} ({type(error).__name__})")
                succeeded = False
            else:
                emit(f"[PASS] name={name} purge doc_id={doc_id}")
    return succeeded


def main(argv=None, *, client_factory=None, emit=print):
    parser = argparse.ArgumentParser(
        description="Run permanent live Mubu write acceptance checks on one isolated document.")
    parser.add_argument("--yes", action="store_true",
                        help="confirm that real document writes and cleanup may run")
    parser.add_argument("--folder", default="0",
                        help="destination folder ID for the temporary document (default: 0)")
    args = parser.parse_args(argv)
    if not args.yes:
        emit("[FAIL] explicit --yes is required; no client was constructed")
        return 2
    return 0 if run_live_write_validation(
        confirmed=True, folder_id=args.folder, client_factory=client_factory,
        emit=emit) else 1


if __name__ == "__main__":
    raise SystemExit(main())
