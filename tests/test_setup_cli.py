"""`sellee setup`, machine half: the gates, the layout it writes, and the daemon gate.

The world is faked at the probe boundary — no package manager, no claude, no supervisor, no live
daemon — but everything between is the real code: real staging, the real symlink swap, the real
job-file render, the real config writes.

The supervisor half is faked as macOS throughout, whichever machine runs the suite, so these
tests are about setup's own orchestration. What differs per OS is covered where it lives: the
gates in test_installer_preflight.py, the systemd mapping in test_supervisor_linux.py.
"""

from __future__ import annotations

import pytest
from tests.test_supervisor import FakePlatform

from sellee import (
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
from sellee.browser import markets as market_adapters
from sellee.installer import checks, materialize, preflight
from sellee.installer import region as region_guess


@pytest.fixture
def world(monkeypatch, xdg_tmp, tree):
    """Every gate passing, a supervisor that only records, and a daemon that comes up."""
    platform = FakePlatform()
    monkeypatch.setattr(setup_cli, "get_platform", lambda: platform)
    monkeypatch.setattr(materialize, "source_tree", lambda: tree)
    monkeypatch.setattr(setup_cli.sys, "platform", "darwin")
    # The window raise is macOS-only, and setup skips its question where it cannot happen — faked
    # so the prompt script is the same whichever machine runs the suite.
    monkeypatch.setattr(setup_cli.foreground, "is_supported", lambda: True)
    monkeypatch.setattr(preflight, "check_platform", lambda: checks.ok("platform", "macOS"))
    monkeypatch.setattr(
        preflight, "check_runtime", lambda tree: checks.ok("python runtime", "3.14.6")
    )
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
    calls = {
        "posts": [],
        "basics": {},
        "markets": [],
        "settings": {},
        "bound": False,
        # Which provider a `bound` channel belongs to — the status route answers for one at a time.
        "adapter": "telegram",
    }

    def fake_post(port, token, route, body, **kwargs):
        calls["posts"].append((route, body))
        if route == "/control/seller-basics":
            from sellee.tools.seller import BasicsError, validate_basics

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
            return 200, {"bound": calls["bound"], "adapter": calls["adapter"]}
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
    monkeypatch.setattr(
        connect_cli, "discord_bind_flow", lambda *a, **k: calls.get("discord_bind_rc", 0)
    )
    monkeypatch.setattr(pass_cli, "harness_config", lambda directory=None: 0)
    monkeypatch.setattr(
        setup_cli, "_provision_rail", lambda ui, region: calls.__setitem__("provisioned", region)
    )
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "Asia/Singapore")

    platform.calls = calls
    return platform


def setup_main(*argv) -> int:
    return cli.main(["sellee", "setup", *argv])


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

    from sellee import __version__

    version_dir = paths.versions_dir() / __version__
    assert (version_dir / "bin" / "sellee").is_file()
    assert materialize.current_version() == __version__
    assert paths.shim_path().is_symlink()

    cfg = config.load()
    assert cfg.daemon_mode == "manual"
    assert cfg.claude_bin == "/opt/claude/bin/claude"

    out = capsys.readouterr().out
    assert "Checking this machine" in out  # the phase headings a reader navigates by
    assert "worker is up" in out


def test_a_container_setup_runs_only_the_half_that_is_about_the_seller(
    world, container, capsys
) -> None:
    """The image already is the machine half: dependencies installed, layout fixed, worker
    started by Docker. What is left is region, rail, marketplaces, Telegram and the workspace."""
    assert setup_main("--yes") == 0

    # Nothing was materialized and no job was registered — the two halves are really separate.
    assert not paths.current().exists()
    assert not paths.shim_path().exists()
    assert list(paths.versions_dir().glob("*")) == []
    assert world.registered_labels == set()
    assert world.register_calls == []

    # And the seller half ran in full.
    assert world.calls["basics"]["region"] == "SG"
    assert world.calls["provisioned"] == "SG"

    out = capsys.readouterr().out
    assert "Checking this machine" not in out
    assert "Installing Sellee" not in out
    assert "already running in this container" in out


def test_a_container_setup_says_where_the_closing_commands_run(world, container, capsys) -> None:
    """They are the same commands as on a host, but the seller has to be inside the container to
    type them — and how they get there is their container runtime's business, not ours."""
    setup_main("--yes")
    out = capsys.readouterr().out
    assert "Run these inside the container" in out
    assert "sellee chat" in out
    assert "rebuild the image" in out  # `sellee update` refuses here
    assert "docker" not in out.lower()


def test_a_container_setup_does_not_promise_that_nothing_was_written(
    world, container, capsys
) -> None:
    """The worker starts with the container and has been writing since — so the host install's
    consent-gate promise would be false here."""
    setup_main("--yes")
    assert "Nothing has been written yet." not in capsys.readouterr().out


def test_setup_announces_every_location_before_writing(world, capsys) -> None:
    setup_main("--yes", "--manual")
    out = capsys.readouterr().out
    for location in (paths.data_root(), paths.config_dir(), paths.cache_dir()):
        assert str(location) in out


def test_manual_mode_starts_the_daemon_and_says_what_manual_costs(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0
    assert world.is_registered("com.sellee.agent")  # started now...
    out = capsys.readouterr().out
    assert "will not restart after you log out" in out  # ...but not after a logout


def test_login_start_mode_registers_the_plist_in_launch_agents(world) -> None:
    assert setup_main("--yes", "--login-start") == 0
    plist = paths.launch_agents_dir(platform=world) / "com.sellee.agent.plist"
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
    assert not world.is_registered("com.sellee.agent")
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
    assert "Sellee" not in captured.out  # no banner, no path preview


def test_a_tree_under_a_protected_folder_stops_setup_before_it_writes(world, capsys) -> None:
    from sellee.installer import preflight as pf

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
    assert "MY isn't a country sellee works in yet" in err
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


def test_a_mistyped_timezone_re_asks_instead_of_ending_the_install(
    world, monkeypatch, capsys
) -> None:
    """A typo in the one free-text field re-asks instead of ending the install, carrying the
    reason and the country's own zone."""
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "")
    # country, a zone that does not exist, Enter for the proposed one, then the defaults.
    _answer(monkeypatch, ["1", "gmt8+", "", "", "", ""])

    assert setup_main("--manual", "--skip-discord") == 0

    assert world.calls["basics"] == {
        "region": "SG",
        "currency": "SGD",
        "timezone": "Asia/Singapore",
    }
    out = capsys.readouterr().out
    assert "unknown timezone 'gmt8+'" in out
    assert "zone names look like Asia/Singapore" in out


def test_a_country_with_one_zone_proposes_it_rather_than_an_empty_field(
    world, monkeypatch, capsys
) -> None:
    """Singapore has exactly one zone, so the question has an answer in it already and Enter is
    enough."""
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "")
    _answer(monkeypatch, ["1", "", "", "", ""])

    assert setup_main("--manual", "--skip-discord") == 0

    assert world.calls["basics"]["timezone"] == "Asia/Singapore"
    assert "Timezone? [Asia/Singapore]" in capsys.readouterr().out


def test_provisioning_gets_the_region_that_was_recorded(world) -> None:
    setup_main("--yes", "--manual")
    assert world.calls["provisioned"] == "SG"


def test_nothing_is_provisioned_without_a_region(world, monkeypatch) -> None:
    monkeypatch.setattr(region_guess, "system_timezone", lambda: "Antarctica/Troll")
    setup_main("--yes", "--manual")
    assert world.calls["provisioned"] is None


# --- marketplaces --------------------------------------------------------------------------------


def _pick(market: str, region: str = "SG") -> str:
    """What to type to choose one marketplace, found by name — the offer is registry-ordered, so
    a hard-coded "1" quietly becomes a different marketplace when one lands ahead of it."""
    from sellee.browser import markets as market_adapters

    return str(market_adapters.connectable_markets(region).index(market) + 1)


def test_picking_a_marketplace_records_the_setting_then_signs_in(world, monkeypatch) -> None:
    # region confirmed, Carousell picked, window question defaulted, no chat channel
    _answer(monkeypatch, ["y", _pick("carousell"), "", ""])

    assert setup_main("--manual", "--skip-discord") == 0

    # The opt-in is recorded before the sign-in, so an interrupted sign-in still leaves the
    # seller's choice standing.
    assert world.calls["settings"] == {"connected_markets": '["carousell"]'}
    assert world.calls["markets"] == ["carousell"]


def test_skipping_the_marketplace_offer_enables_nothing(world, monkeypatch, capsys) -> None:
    # region confirmed, no marketplace picked, window question defaulted, no chat channel
    _answer(monkeypatch, ["y", "", "", ""])

    assert setup_main("--manual", "--skip-discord") == 0

    assert world.calls["settings"] == {}
    assert world.calls["markets"] == []
    assert "carousell.ai only" in capsys.readouterr().out


def test_skip_markets_never_offers(world) -> None:
    assert setup_main("--yes", "--manual", "--skip-markets") == 0
    assert world.calls["markets"] == []


def test_a_us_seller_is_offered_facebook_rather_than_told_there_is_nothing(
    world, monkeypatch, capsys
) -> None:
    """Carousell runs no US site; Facebook serves everywhere, making it the one marketplace a US
    seller can have."""
    # region confirmed, nothing picked, window question defaulted, no chat channel
    _answer(monkeypatch, ["y", "", "", ""])

    assert setup_main("--manual", "--skip-discord", "--region", "US") == 0

    out = capsys.readouterr().out
    assert "Facebook Marketplace" in out
    assert "none available in this region" not in out


def test_a_region_with_no_marketplaces_says_so_rather_than_offering_an_empty_list(
    world, capsys, monkeypatch
) -> None:
    """Pinned rather than reached through a region, because no region has nothing any more."""
    monkeypatch.setattr(market_adapters, "connectable_markets", lambda region: [])

    assert setup_main("--yes", "--manual", "--region", "US") == 0
    assert "none available in this region" in capsys.readouterr().out


# --- the browser window --------------------------------------------------------------------------


def test_declining_the_browser_window_question_records_the_setting(world, monkeypatch) -> None:
    # region confirmed, no marketplace, window declined, no chat channel
    _answer(monkeypatch, ["y", "", "n", ""])
    assert setup_main("--manual", "--skip-discord") == 0
    assert world.calls["settings"]["raise_browser"] == "false"


def test_accepting_the_browser_window_question_writes_nothing(world, monkeypatch) -> None:
    """The default lives in code; writing it back would list the setting as customized on the
    /sellee card."""
    _answer(monkeypatch, ["y", "", "", ""])
    assert setup_main("--manual", "--skip-discord") == 0
    assert "raise_browser" not in world.calls["settings"]


def test_the_browser_question_comes_after_the_marketplace_sign_in(
    world, monkeypatch, capsys
) -> None:
    """The first window of an install must always come forward — a sign-in window the seller
    cannot find is a wall — so the question that could turn that off is asked only afterwards."""
    _answer(monkeypatch, ["y", _pick("carousell"), "n", ""])
    assert setup_main("--manual", "--skip-discord") == 0
    assert world.calls["markets"] == ["carousell"]  # signed in under the raise-by-default
    out = capsys.readouterr().out
    assert out.index("Other marketplaces") < out.index("Keep bringing my window to the front?")


def test_the_browser_question_names_the_settings_toggle(world, capsys) -> None:
    assert setup_main("--yes", "--manual") == 0
    out = capsys.readouterr().out
    assert "raise_browser" in out  # the way back to the toggle is part of the phase


def test_an_os_that_cannot_raise_a_window_is_never_asked(world, monkeypatch, capsys) -> None:
    """Offering to turn off a behavior the OS cannot perform teaches the seller something untrue
    about their install — and would put a question in the script that has no answer to give."""
    monkeypatch.setattr(setup_cli.foreground, "is_supported", lambda: False)
    _answer(monkeypatch, ["y", "", ""])  # region, no marketplace, channel — no window question
    assert setup_main("--manual", "--skip-discord") == 0
    out = capsys.readouterr().out
    assert "Browser window" not in out
    assert "raise_browser" not in world.calls["settings"]


def test_a_container_setup_is_never_asked_about_the_window(world, container, capsys) -> None:
    """Chrome runs on the seller's own desktop there, so nothing setup could offer to raise.

    The world fakes an OS that *can* raise, which is the point: what excludes a container is the
    deployment profile, not the incidental fact that the image happens to be a Linux one.
    """
    assert setup_main("--yes", "--manual") == 0
    assert "Browser window" not in capsys.readouterr().out


# --- the chat channel ----------------------------------------------------------------------------


def test_the_menu_lists_every_channel_so_neither_is_hidden_behind_the_other(
    world, monkeypatch, capsys
) -> None:
    """The reason this is a menu: only one channel binds, so a yes/no on Telegram first decided
    Discord too — accepting Telegram made Discord look unavailable, and a seller who had never
    heard of Discord never learned it was an option."""
    _answer(monkeypatch, ["y", "", "", ""])  # region, no marketplace, window default, no channel
    assert setup_main("--manual") == 0
    out = capsys.readouterr().out
    assert "Chat channel" in out
    assert "1) Telegram" in out and "2) Discord" in out
    assert "Only one channel can be" in out  # the constraint is stated before the choice


def test_picking_from_the_menu_runs_that_channel_s_bind_flow(world, monkeypatch) -> None:
    ran = []
    monkeypatch.setattr(connect_cli, "bind_flow", lambda *a, **k: ran.append("telegram") or 0)
    monkeypatch.setattr(
        connect_cli, "discord_bind_flow", lambda *a, **k: ran.append("discord") or 0
    )
    _answer(monkeypatch, ["y", "", "", "2"])  # ... then Discord, the second entry
    assert setup_main("--manual") == 0
    assert ran == ["discord"]


def test_an_empty_answer_binds_nothing_and_names_both_verbs(world, monkeypatch, capsys) -> None:
    ran = []
    monkeypatch.setattr(connect_cli, "bind_flow", lambda *a, **k: ran.append("telegram") or 0)
    _answer(monkeypatch, ["y", "", "", ""])
    assert setup_main("--manual") == 0
    assert ran == []
    out = capsys.readouterr().out
    assert "sellee connect telegram" in out and "sellee connect discord" in out


def test_a_failed_bind_points_at_the_verb_that_resumes_it(world, monkeypatch, capsys) -> None:
    monkeypatch.setattr(connect_cli, "discord_bind_flow", lambda *a, **k: 1)
    _answer(monkeypatch, ["y", "", "", "2"])
    assert setup_main("--manual") == 0
    assert "resume later with `sellee connect discord`" in capsys.readouterr().out


def test_a_piped_install_picks_no_channel_and_says_so(world, capsys) -> None:
    # Nothing is chosen for a seller who is not there to choose: binding a channel takes a
    # credential and a phone, so --yes and a pipe both mean "none", not "the first one".
    assert setup_main("--yes", "--manual") == 0
    out = capsys.readouterr().out
    assert "skipped — connect later with" in out
    assert "sellee connect telegram" in out


def test_an_already_bound_channel_is_named_rather_than_re_offered(world, capsys) -> None:
    world.calls["bound"] = True
    assert setup_main("--yes", "--manual") == 0
    out = capsys.readouterr().out
    assert "already connected: Telegram" in out
    assert "Connect now?" not in out


def test_an_already_bound_discord_channel_is_named_as_discord(world, capsys) -> None:
    """The status route answers for whichever provider holds the singleton row, so this is the
    case that used to read as Telegram — or as both channels being connected at once."""
    world.calls["bound"] = True
    world.calls["adapter"] = "discord"
    assert setup_main("--yes", "--manual") == 0
    out = capsys.readouterr().out
    assert "already connected: Discord" in out
    assert "already connected: Telegram" not in out


def test_skipping_one_channel_leaves_a_menu_of_the_other(world, monkeypatch, capsys) -> None:
    _answer(monkeypatch, ["y", "", "", ""])
    assert setup_main("--manual", "--skip-telegram") == 0
    out = capsys.readouterr().out
    assert "1) Discord" in out
    assert ") Telegram" not in out  # not an entry at all


def test_skipping_both_channels_skips_the_step_entirely(world, capsys) -> None:
    assert setup_main("--yes", "--manual", "--skip-telegram", "--skip-discord") == 0
    assert "Chat channel" not in capsys.readouterr().out


def test_finish_points_a_bound_install_at_the_phone(world, capsys) -> None:
    world.calls["bound"] = True
    assert setup_main("--yes", "--manual") == 0
    out = capsys.readouterr().out
    assert "Next: your first listing." in out
    assert "Open Telegram" in out and "photo" in out


def test_finish_points_a_discord_install_at_discord(world, capsys) -> None:
    world.calls["bound"] = True
    world.calls["adapter"] = "discord"
    assert setup_main("--yes", "--manual") == 0
    out = capsys.readouterr().out
    assert "Open Discord" in out and "photo" in out
    assert "Open Telegram" not in out  # the app the seller actually connected


def test_finish_points_an_unbound_install_at_the_terminal(world, capsys) -> None:
    assert setup_main("--yes", "--manual", "--skip-telegram", "--skip-discord") == 0
    out = capsys.readouterr().out
    assert "Next: your first listing." in out
    assert "/sell" in out
    assert "Open Telegram" not in out  # no phone to send the photo to


# --- the attended workspace ----------------------------------------------------------------------


def test_the_attended_workspace_lands_at_a_documented_path(world, monkeypatch, capsys) -> None:
    written = []
    monkeypatch.setattr(
        pass_cli, "harness_config", lambda directory=None: written.append(directory) or 0
    )
    assert setup_main("--yes", "--manual") == 0
    assert written == [paths.data_root() / "attended"]
    # The seller is pointed at the command, not at the directory it happens to use.
    assert "sellee chat" in capsys.readouterr().out


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
    config.merge_into_file({"daemon_label": "com.sellee.agent.dev"})

    assert setup_main("--yes", "--manual") == 0

    assert (paths.config_dir() / "com.sellee.agent.dev.plist").exists()
    assert not (paths.config_dir() / "com.sellee.agent.plist").exists()


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
    plist = (paths.config_dir() / "com.sellee.agent.plist").read_text()
    assert "/opt/node-versions/v22/bin" in plist


def test_setup_records_every_directory_the_worker_needs_not_just_the_first(
    world, monkeypatch
) -> None:
    # Where `node` and `npx` live apart, both directories are recorded and both reach the job.
    fragment = "/opt/node/bin:/usr/local/npm-global/bin"
    monkeypatch.setattr(preflight, "node_path_fragment", lambda: fragment)

    assert setup_main("--yes", "--manual") == 0

    assert config.load().node_bin_dir == fragment
    assert fragment in (paths.config_dir() / "com.sellee.agent.plist").read_text()


# --- the gate that only exists on one OS ---------------------------------------------------------


def test_linux_is_asked_for_a_systemd_user_manager_before_anything_slow(world, monkeypatch) -> None:
    """Without a user manager there is nothing to register the worker with, so this has to stop
    the run rather than let it get as far as `daemon install` failing in systemd's own words."""
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(
        preflight,
        "check_systemd_user",
        lambda: checks.fail("systemd", "no user manager", "log in at the machine"),
    )
    assert setup_main("--yes", "--manual") == 1
    assert not paths.current().exists()  # stopped before it wrote the layout


def test_macos_is_never_asked_about_systemd(world, monkeypatch) -> None:
    def explode():
        raise AssertionError("a Mac was asked about systemd")

    monkeypatch.setattr(preflight, "check_systemd_user", explode)
    assert setup_main("--yes", "--manual") == 0


def test_a_missing_dependency_off_macos_is_fatal_rather_than_offered(world, monkeypatch) -> None:
    """Installing a system package on Linux needs sudo, and an installer must not wield that on
    someone's behalf — so the fix is printed and the run stops."""
    monkeypatch.setattr(setup_cli.sys, "platform", "linux")
    monkeypatch.setattr(preflight, "check_systemd_user", lambda: checks.ok("systemd", "running"))
    monkeypatch.setattr(
        preflight, "check_node", lambda: checks.fail("node", "not installed", "sudo apt install")
    )

    def explode(*args, **kwargs):
        raise AssertionError("a package manager was run on the seller's behalf")

    monkeypatch.setattr(preflight, "brew_install", explode)
    monkeypatch.setattr(preflight, "homebrew_path", lambda: "/opt/homebrew/bin/brew")

    assert setup_main("--yes", "--manual") == 1
