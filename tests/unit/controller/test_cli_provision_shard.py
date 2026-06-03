from __future__ import annotations


def test_provision_seed_parser_has_shard_flag():
    from mthydra.controller.cli import build_parser
    p = build_parser()
    ns = p.parse_args(["provision-seed", "--provider", "tw", "--region", "ru",
                       "--shard", "s-hi",
                       "--agent-source-url", "https://example.com/agent.tar",
                       "--agent-source-sha256", "abc123",
                       "--descriptor-refresh-url", "https://example.com/desc"])
    assert ns.shard_id == "s-hi"
