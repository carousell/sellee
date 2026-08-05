"""`selly-agent setup`, machine half: the gates, the layout it writes, and the daemon gate.

The world is faked at the probe boundary — no brew, no claude, no launchctl, no live daemon —
but everything between is the real code: real staging, the real symlink swap, the real plist
render, the real config writes.
"""

from __future__ import annotations

import pytest
from tests.test_supervisor import FakePlatform

from selly_agent import (
    cli,
    config,
    connect_cli,
    control,
    heartbeat,
    pass_cli,
    passes,
    paths,
    secrets,
    settings_cli,
    setup_cli,
)
from selly_agent.installer import checks, materialize, preflight
from selly_agent.installer import region as region_guess


@pytest.fixture
def world(monkeypatch, xdg_tmp, tree):
    """Every gate passing, a launchctl that only records, and a daemon that comes up."""
    platform = FakePlatform()
    monkeypatch.setattr(setup_cli, "get_platform", lambda: platform)
    monkeypatch.setattr(materialize, "source_tree", lambda: tree)
    monkeypatch.setattr(preflight, "check_platform", lambda: checks.ok("platform", "macOS"))
    monkeypatch.setattr(preflight, "check_node", lambda: checks.ok("node", "v22"))
    monkeypatch.setattr(preflight, "check_chrome", lambda chrome_bin=None: checks.ok("chrome", "-"))
    monkeypatch.setattr(preflight, "check_claude", lambda cfg: checks.ok("claude CLI", "signed in"))
    monkeypatch.setattr(
        preflight, "check_supervised_spawn", lambda cfg: checks.ok("browser server", "-")
    )
    monkeypatch.setattr(preflight, "prewarm_playwright", lambda cfg: checks.ok("playwright", "-"))
    monkeypatch.setattr(preflight, "agent_context", lambda env=None: "")
    monkeypatch.setattr(passes, "resolve_claude_bin", lambda cfg: "/opt/claude/bin/claude")
    monkeypatch.setattr(heartbeat, "wait_fresh", lambda path, **kwargs: True)
    # A PATH that already has ~/.local/bin, so the rc-file offer stays out of the way unless a
    # test is about it.
    monkeypatch.setenv("PATH", f"/usr/bin:{paths.user_bin_dir()}")

    # The daemon half: a token as if one had been minted at first start, and control routes that
    # record rather than serve.
    paths.ensure_config_dir()
    secrets.write_secret(paths.mcp_token_path(), "attended-secret")
    calls = {"posts": [], "basics": {}, "markets": [], "settings": {}, "bound": False}

    def fake_post(port, token, route, body, **kwargs):
        calls["posts"].append((route, body))
        if route == "/control/seller-basics":
            from selly_agent.tools.seller import BasicsError, validate_basics

            try:
                checked = validate_basics(body)
            except BasicsError as exc:
                return 400, {"error": str(exc)}
            calls["basics"] = {**calls["basics"], **checked}
            return 200, {"basics": calls["basics"]}
        return 200, {}

    def fake_get(port, token, route, params=None, **kwargs):
        if route == "/control/seller-basics":
            return 200, {"basics": calls["basics"]}
        if route == "/control/channel-status":
            return 200, {"bound": calls["bound"]}
        return 200, {}

    def fake_set_setting(port, token, key, value):
        calls["settings"][key] = value
        return 0

    def fake_market_flow(port, token, market, **kwargs):
        calls["markets"].append(market)
        return 0

    monkeypatch.setattr(control, "post", fake_post)
    monkeypatch.setattr(control, "get", fake_get)
    monkeypatch.setattr(settings_cli, "set_setting", fake_set_setting)
    monkeypatch.setattr(connect_cli, "market_flow", fake_market_flow)
    monkeypatch.setattr(connect_cli, "bind_flow", lambda *a, **k: calls.get("bind_rc", 0))
    monkeypatch.setattr(pass_cli, "harness_config", lambda directory=None: 0)
    monkeypatch.setattr(
        setup_cli, "_provision_rail", lambda ui, region: calls.__setitem__("provisioned", region)
    )
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "Asia/Singapore")

    platform.calls = calls
    return platform


def setup_main(*argv) -> int:
    return cli.main(["selly-agent", "setup", *argv])


def _answer(monkeypatch, replies, *, consent: bool = True):
    """Drive the prompts with a script, as a person at a real terminal would.

    The consent gate is the first question of every interactive run, so it is answered here and
    each test's script covers only the questions that test is about. `consent=False` hands the
    gate back to the script, for the tests about declining it.

    Exhausting the script raises rather than blocking, so a prompt nobody anticipated shows up as
    a failure instead of a hung suite.
    """
    monkeypatch.setattr(setup_cli.Ui, "_detect_interactive", lambda self: True)
    answers = iter((["y"] if consent else []) + list(replies))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))


# --- the happy path ----------------------------------------------------------------------------


def test_a_default_install_stages_a_version_and_brings_the_daemon_up(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0

    from selly_agent import __version__

    version_dir = paths.versions_dir() / __version__
    assert (version_dir / "bin" / "selly-agent").is_file()
    assert materialize.current_version() == __version__
    assert paths.shim_path().is_symlink()

    cfg = config.load()
    assert cfg.daemon_mode == "manual"
    assert cfg.claude_bin == "/opt/claude/bin/claude"

    out = capsys.readouterr().out
    assert "Checking this machine" in out  # the phase headings a reader navigates by
    assert "worker is up" in out


def test_setup_announces_every_location_before_writing(world, capsys) -> None:
    setup_main("--yes", "--manual")
    out = capsys.readouterr().out
    for location in (paths.data_root(), paths.config_dir(), paths.cache_dir()):
        assert str(location) in out


def test_manual_mode_starts_the_daemon_and_says_what_manual_costs(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0
    assert world.is_registered("com.selly.agent")  # started now...
    out = capsys.readouterr().out
    assert "will not restart after you log out" in out  # ...but not after a logout


def test_login_start_mode_registers_the_plist_in_launch_agents(world) -> None:
    assert setup_main("--yes", "--login-start") == 0
    plist = paths.launch_agents_dir(platform=world) / "com.selly.agent.plist"
    assert plist.exists()
    assert config.load().daemon_mode == "login-start"


def test_dev_mode_points_current_at_the_tree_instead_of_copying(world, tree) -> None:
    assert setup_main("--yes", "--manual", "--dev") == 0
    assert materialize.current_target() == tree.resolve()
    assert materialize.is_dev_install() is True
    assert not (paths.versions_dir() / "0.1.0.dev0").exists()


def test_a_re_run_is_idempotent(world) -> None:
    assert setup_main("--yes", "--manual") == 0
    assert setup_main("--yes", "--manual") == 0
    assert materialize.current_version() is not None
    assert paths.shim_path().is_symlink()


# --- the consent gate ---------------------------------------------------------------------------


def test_declining_the_gate_leaves_the_machine_untouched(world, monkeypatch, capsys) -> None:
    _answer(monkeypatch, ["n"], consent=False)

    assert setup_main("--manual") == 0  # declining is an answer, not a failure

    assert not paths.versions_dir().exists()
    assert not paths.current().exists()
    assert not paths.shim_path().exists()
    assert not world.is_registered("com.selly.agent")
    assert "nothing was written" in capsys.readouterr().out


def test_nothing_touches_the_machine_before_the_gate_is_answered(
    world, monkeypatch, capsys
) -> None:
    # The gate is only worth having if it precedes everything that changes the machine — which
    # includes the dependency checks, since those offer to install a package and to download one.
    ran = []
    monkeypatch.setattr(
        preflight, "check_node", lambda: ran.append("node") or checks.ok("node", "v22")
    )
    monkeypatch.setattr(
        preflight,
        "prewarm_playwright",
        lambda cfg: ran.append("playwright") or checks.ok("playwright", "-"),
    )
    _answer(monkeypatch, ["n"], consent=False)

    assert setup_main("--manual") == 0
    assert ran == []


def test_the_gate_states_the_locations_before_asking(world, monkeypatch, capsys) -> None:
    _answer(monkeypatch, ["n"], consent=False)
    setup_main("--manual")

    out = capsys.readouterr().out
    before_question, _, _ = out.partition("Proceed with the installation?")
    for location in (paths.data_root(), paths.config_dir(), paths.cache_dir()):
        assert str(location) in before_question


# --- gates -------------------------------------------------------------------------------------


def test_an_unsupported_platform_is_one_honest_line_not_a_banner(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        preflight, "check_platform", lambda: checks.fail("platform", "linux", "macOS only")
    )
    assert setup_main("--yes") == 1
    captured = capsys.readouterr()
    assert "linux" in captured.err
    assert "Selly" not in captured.out  # no banner, no path preview


def test_a_tree_under_a_protected_folder_stops_setup_before_it_writes(world, capsys) -> None:
    from selly_agent.installer import preflight as pf

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            pf,
            "check_tree_location",
            lambda tree: checks.fail("install location", "under ~/Documents", "Move it"),
        )
        assert setup_main("--yes", "--manual") == 1
    assert not paths.current().exists()
    assert "Move it" in capsys.readouterr().err


def test_a_signed_out_claude_offers_the_login_flow_and_re_probes(
    world, monkeypatch, capsys
) -> None:
    answers = iter(
        [
            checks.fail("claude CLI", "installed but signed out", "claude auth login"),
            checks.ok("claude CLI", "signed in"),
        ]
    )
    monkeypatch.setattr(preflight, "check_claude", lambda cfg: next(answers))
    logins = []
    monkeypatch.setattr(preflight, "claude_login", lambda cfg: logins.append(cfg) or 0)
    monkeypatch.setattr(setup_cli.Ui, "_detect_interactive", lambda self: True)

    assert setup_main("--yes", "--manual") == 0
    assert len(logins) == 1
    assert "Running `claude auth login`" in capsys.readouterr().out


def test_a_signed_out_claude_with_no_terminal_stops_rather_than_pretending(
    world, monkeypatch, capsys
) -> None:
    # `--yes` is not a person: the login flow prints a URL and reads back a pasted code.
    monkeypatch.setattr(
        preflight,
        "check_claude",
        lambda cfg: checks.fail("claude CLI", "signed out", "claude auth login"),
    )
    logins = []
    monkeypatch.setattr(preflight, "claude_login", lambda cfg: logins.append(cfg) or 0)

    assert setup_main("--yes", "--manual") == 1
    assert logins == []
    assert "claude auth login" in capsys.readouterr().err


def test_a_missing_claude_cli_is_fatal_and_never_installed_for_you(world, monkeypatch) -> None:
    monkeypatch.setattr(
        preflight, "check_claude", lambda cfg: checks.fail("claude CLI", "not installed", "curl …")
    )
    monkeypatch.setattr(passes, "resolve_claude_bin", lambda cfg: None)
    assert setup_main("--yes", "--manual") == 1


def test_a_missing_dependency_offers_brew_then_re_probes(world, monkeypatch, capsys) -> None:
    probes = iter(
        [
            checks.fail("node", "not installed", "brew install node"),
            checks.ok("node", "v22 at /opt/homebrew/bin/node"),
        ]
    )
    monkeypatch.setattr(preflight, "check_node", lambda: next(probes))
    monkeypatch.setattr(preflight, "homebrew_path", lambda: "/opt/homebrew/bin/brew")
    installed = []
    monkeypatch.setattr(
        preflight,
        "brew_install",
        lambda package, cask=False: (installed.append((package, cask)), (True, ""))[1],
    )

    assert setup_main("--yes", "--manual") == 0
    assert installed == [("node", False)]
    assert "brew install node" in capsys.readouterr().out


def test_without_homebrew_a_missing_dependency_is_fatal_and_brew_is_never_bootstrapped(
    world, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        preflight, "check_node", lambda: checks.fail("node", "not installed", "brew install node")
    )
    monkeypatch.setattr(preflight, "homebrew_path", lambda: "")

    assert setup_main("--yes", "--manual") == 1
    err = capsys.readouterr().err
    assert "https://brew.sh" in err
    assert not paths.current().exists()


def test_a_failing_prewarm_is_a_warning_not_a_stop(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        preflight, "prewarm_playwright", lambda cfg: checks.warn("playwright", "offline")
    )
    assert setup_main("--yes", "--manual") == 0
    assert "⚠️ playwright" in capsys.readouterr().out


def test_a_browser_server_the_worker_cannot_spawn_stops_setup(world, monkeypatch, capsys) -> None:
    # The download can fail and still leave a working install; a spawn the worker cannot perform
    # cannot — it is a dead browser lane at the first publish, with nothing said about it.
    monkeypatch.setattr(
        preflight,
        "check_supervised_spawn",
        lambda cfg: checks.fail("browser server", "npx is unreachable", "re-run ./setup"),
    )

    assert setup_main("--yes", "--manual") == 1
    assert "npx is unreachable" in capsys.readouterr().err
    assert not paths.current().exists()


def test_an_agent_session_stops_setup_asking_questions(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(preflight, "agent_context", lambda env=None: "CLAUDECODE")
    monkeypatch.setattr(setup_cli.Ui, "_detect_interactive", lambda self: True)

    assert setup_main("--manual") == 0
    assert "inside an agent session" in capsys.readouterr().out


# --- the daemon gate -----------------------------------------------------------------------------


def test_a_daemon_that_never_heartbeats_fails_with_its_own_log(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(heartbeat, "wait_fresh", lambda path, **kwargs: False)
    paths.ensure_state_dirs()
    (paths.logs_dir() / "agent.err.log").write_text("Traceback…\nOSError: port 7355 in use\n")

    assert setup_main("--yes", "--manual") == 1
    err = capsys.readouterr().err
    assert "didn't start" in err
    assert "port 7355 in use" in err


# --- PATH -----------------------------------------------------------------------------------


def test_a_missing_path_entry_is_offered_and_written_to_the_rc_file(
    world, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert setup_main("--yes", "--manual") == 0

    rc = materialize.shell_rc_target("/bin/zsh")
    assert materialize.rc_block_present(rc.read_text())
    assert "is not on your PATH" in capsys.readouterr().out


def test_no_modify_path_prints_the_line_and_leaves_the_rc_file_alone(
    world, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert setup_main("--yes", "--manual", "--no-modify-path") == 0

    assert not materialize.shell_rc_target("/bin/zsh").exists()
    assert materialize.RC_BLOCK_BODY in capsys.readouterr().out


def test_a_piped_run_that_never_said_yes_gets_the_line_not_an_edit(
    world, monkeypatch, capsys
) -> None:
    # No terminal to ask at and no --yes: nobody consented to a dotfile edit.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    assert setup_main("--manual") == 0

    assert not materialize.shell_rc_target("/bin/zsh").exists()
    assert materialize.RC_BLOCK_BODY in capsys.readouterr().out


# --- where the seller sells -------------------------------------------------------------------


def test_the_region_is_proposed_from_the_machines_timezone(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0
    assert world.calls["basics"] == {
        "region": "SG",
        "currency": "SGD",
        "timezone": "Asia/Singapore",
    }
    assert "You sell in SG · SGD · Asia/Singapore, correct?" in capsys.readouterr().out


def test_the_region_flag_wins_over_the_guess(world) -> None:
    assert setup_main("--yes", "--manual", "--region", "us") == 0
    assert world.calls["basics"]["region"] == "US"
    assert world.calls["basics"]["currency"] == "USD"


def test_a_country_the_rail_does_not_serve_is_refused(world, capsys) -> None:
    # Storing it would produce a seller who looks configured and is not: nothing they list can
    # reach the rail, and the rail is where every listing goes.
    assert setup_main("--yes", "--manual", "--region", "my") == 1
    assert world.calls["basics"] == {}
    err = capsys.readouterr().err
    assert "MY isn't a country selly-agent works in yet" in err
    assert "SG, US" in err


def test_a_re_run_leaves_a_recorded_region_alone(world, capsys) -> None:
    setup_main("--yes", "--manual")
    capsys.readouterr()
    assert setup_main("--yes", "--manual") == 0
    assert "unchanged" in capsys.readouterr().out


def test_a_timezone_outside_the_supported_countries_makes_no_guess(
    world, monkeypatch, capsys
) -> None:
    # Kuala Lumpur is a real timezone the rail does not serve. Proposing "MY · MYR" would be a
    # confident answer setup then has to refuse, so it proposes nothing and asks instead.
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "Asia/Kuala_Lumpur")
    assert setup_main("--yes", "--manual") == 0
    assert world.calls["basics"] == {}
    out = capsys.readouterr().out
    assert "No region recorded" in out


def test_provisioning_gets_the_region_that_was_recorded(world) -> None:
    setup_main("--yes", "--manual")
    assert world.calls["provisioned"] == "SG"


def test_nothing_is_provisioned_without_a_region(world, monkeypatch) -> None:
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "Antarctica/Troll")
    setup_main("--yes", "--manual")
    assert world.calls["provisioned"] is None


# --- marketplaces --------------------------------------------------------------------------------


def test_picking_a_marketplace_records_the_setting_then_signs_in(world, monkeypatch) -> None:
    _answer(monkeypatch, ["y", "1", "n"])  # region confirmed, Carousell picked, Telegram declined

    assert setup_main("--manual") == 0

    # The opt-in is recorded before the sign-in, so an interrupted sign-in still leaves the
    # seller's choice standing.
    assert world.calls["settings"] == {"crosslist_markets": '["carousell"]'}
    assert world.calls["markets"] == ["carousell"]


def test_skipping_the_marketplace_offer_enables_nothing(world, monkeypatch, capsys) -> None:
    _answer(monkeypatch, ["y", "", "n"])  # region confirmed, no marketplace picked

    assert setup_main("--manual") == 0

    assert world.calls["settings"] == {}
    assert world.calls["markets"] == []
    assert "carousell.ai only" in capsys.readouterr().out


def test_skip_markets_never_offers(world) -> None:
    assert setup_main("--yes", "--manual", "--skip-markets") == 0
    assert world.calls["markets"] == []


def test_a_region_with_no_marketplaces_says_so_rather_than_offering_an_empty_list(
    world, capsys
) -> None:
    assert setup_main("--yes", "--manual", "--region", "US") == 0
    assert "none available in this region" in capsys.readouterr().out


# --- Telegram ------------------------------------------------------------------------------------


def test_telegram_is_offered_and_declining_points_at_the_verb(world, capsys) -> None:
    # Not interactive, so the offer is declined for us — the path a piped install takes.
    assert setup_main("--manual") == 0
    assert "selly-agent connect telegram" in capsys.readouterr().out


def test_an_already_bound_channel_is_not_offered_again(world, capsys) -> None:
    world.calls["bound"] = True
    assert setup_main("--yes", "--manual") == 0
    assert "already connected" in capsys.readouterr().out


def test_skip_telegram_never_offers(world, capsys) -> None:
    assert setup_main("--yes", "--manual", "--skip-telegram") == 0
    assert "Connect Telegram now?" not in capsys.readouterr().out


# --- the attended workspace ----------------------------------------------------------------------


def test_the_attended_workspace_lands_at_a_documented_path(world, monkeypatch, capsys) -> None:
    written = []
    monkeypatch.setattr(
        pass_cli, "harness_config", lambda directory=None: written.append(directory) or 0
    )
    assert setup_main("--yes", "--manual") == 0
    assert written == [paths.data_root() / "attended"]
    # The seller is pointed at the command, not at the directory it happens to use.
    assert "selly-agent chat" in capsys.readouterr().out


def test_a_re_run_stops_the_old_worker_before_replacing_its_code(world, capsys) -> None:
    # `launchctl bootstrap` is a no-op on a label that is already loaded, so without stopping
    # first the old process keeps running — still writing heartbeats, so the wait below passes —
    # while setup reports the new version as up.
    setup_main("--yes", "--manual")
    capsys.readouterr()

    assert setup_main("--yes", "--manual") == 0

    out = capsys.readouterr().out
    assert "stopping the running worker" in out


def test_a_custom_daemon_label_is_not_replaced_by_the_default_one(world) -> None:
    # Installing under the default label while a custom-labelled job is loaded leaves two plists
    # and two daemons writing one database.
    config.merge_into_file({"daemon_label": "com.selly.agent.dev"})

    assert setup_main("--yes", "--manual") == 0

    assert (paths.config_dir() / "com.selly.agent.dev.plist").exists()
    assert not (paths.config_dir() / "com.selly.agent.plist").exists()


def test_fish_is_told_the_fish_command_and_its_config_is_left_alone(
    world, monkeypatch, capsys
) -> None:
    # Writing ~/.profile for fish produces a file fish never reads, holding a line it could not
    # parse — a confident success message and no effect.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/usr/local/bin/fish")

    assert setup_main("--yes", "--manual") == 0

    out = capsys.readouterr().out
    assert "fish_add_path ~/.local/bin" in out
    assert "config is left unchanged" in out
    assert not (paths.user_path("~") / ".profile").exists()
    assert materialize.RC_BLOCK_BODY not in out  # never the POSIX line for fish


def test_an_unrecognised_shell_is_named_and_told_what_to_add(world, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/ksh")

    assert setup_main("--yes", "--manual") == 0

    out = capsys.readouterr().out
    assert "Shell: ksh." in out
    assert str(paths.user_bin_dir()) in out
    assert not (paths.user_path("~") / ".profile").exists()


def test_an_unset_shell_says_so_rather_than_naming_nothing(world, monkeypatch, capsys) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("SHELL", raising=False)

    assert setup_main("--yes", "--manual") == 0

    assert "shell could not be determined" in capsys.readouterr().out


# --- what the supervised worker needs pinned ----------------------------------------------------


def test_setup_pins_the_node_directory_into_the_workers_job(world, monkeypatch) -> None:
    # The ordering is the point: the directory is recorded while a real shell's PATH is available,
    # and the job definition is rendered afterwards, so the worker is started able to find npx.
    monkeypatch.setattr(preflight, "node_path_fragment", lambda: "/opt/node-versions/v22/bin")

    assert setup_main("--yes", "--manual") == 0

    assert config.load().node_bin_dir == "/opt/node-versions/v22/bin"
    plist = (paths.config_dir() / "com.selly.agent.plist").read_text()
    assert "/opt/node-versions/v22/bin" in plist


def test_setup_records_every_directory_the_worker_needs_not_just_the_first(
    world, monkeypatch
) -> None:
    # Where `node` and `npx` live apart, both directories are recorded and both reach the job.
    fragment = "/opt/node/bin:/usr/local/npm-global/bin"
    monkeypatch.setattr(preflight, "node_path_fragment", lambda: fragment)

    assert setup_main("--yes", "--manual") == 0

    assert config.load().node_bin_dir == fragment
    assert fragment in (paths.config_dir() / "com.selly.agent.plist").read_text()
