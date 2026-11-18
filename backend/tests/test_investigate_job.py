"""Working out why something does not work, as a job (jobs/investigate.py, diagnose.py): look first, keep what was seen, test one cause at a time, offer only what is supported."""

from __future__ import annotations

import pytest

from agent_control import diagnose
from agent_control.diagnose import Probe, is_private, parse_mdns, parse_ssdp
from agent_control.goals import Goal, GoalStatus
from agent_control.jobs import engines, investigate, research
from agent_control.jobs.model import Job, JobStatus
from agent_control.jobs.research import Page, ResearchIO
from agent_control.jobs.runner import JobRunner
from helpers import make_repos

NETWORK = "Network adapters:\n- Ethernet (physical): connected, address 192.168.1.104, network 'home' is Public.\nEthernet is on a Public network ('home'). Windows turns network discovery off for Public networks."
DEVICES = "Searched from 192.168.1.104 (2 answered).\n- 192.168.1.224: LG TV; mDNS _googlecast._tcp.local\n- 192.168.1.72: Dining Room TV (Google Eureka Dongle)"
PAGE = "Windows turns network discovery off on a Public network, so a device cannot be found until the network is set to Private. " * 4


class _Fake:
    """A model that answers as a good technician would, scripted from what it is shown, and probes that return fixed results."""

    def __init__(self, *, extra_probe: dict | None = None) -> None:
        self.ran: list[tuple[str, dict]] = []
        self.asked: list[str] = []
        self.extra_probe = extra_probe

    async def think(self, system, user, schema):
        self.asked.append(schema.__name__)
        if schema is investigate._Plan:
            return investigate._Plan(probes=["network_overview", "lan_devices", "not_a_check", "network_overview"], hypotheses=["The network is Public so discovery is off", "The TV is off", "A firewall blocks discovery"])
        if schema is investigate._Analysis:
            if "lan_devices" in user and "Findings so far:\n(none yet)" in user:
                return investigate._Analysis(
                    findings=[investigate._Finding(text="Ethernet is on a Public network.", probe="network_overview"), investigate._Finding(text="Two devices answer on 192.168.1.", probe="lan_devices"),
                              investigate._Finding(text="Something nothing showed.", probe="firewall")],  # not run: dropped
                    hypotheses=[investigate._Hypothesis(id="H1", status="supported"), investigate._Hypothesis(id="H9", status="supported")],
                    next=investigate._Next(op="host_check", host=self.extra_probe["host"] if self.extra_probe else "192.168.1.224"),
                )
            return investigate._Analysis(enough=True, hypotheses=[investigate._Hypothesis(id="H2", status="refuted"), investigate._Hypothesis(id="H3", status="open")])
        if schema is investigate._Queries:
            return investigate._Queries(searches=["TV does not show up in Chrome cast Public network", "second search"])
        if schema is research._Notes:
            return research._Notes(facts=[research._Fact(claim="Discovery is off on a Public network.", quote="Windows turns network discovery off on a Public network, so a device cannot be found")])
        if schema is investigate._Options:
            return investigate._Options(options=[
                investigate._Option(title="Set the network to Private", why="Discovery is off while it is Public.", how="Settings > Network > Ethernet > Private", changes_something=True, undo="Set it back to Public", evidence=["F1", "W1"]),
                investigate._Option(title="Buy a new TV", why="Because.", how="Shop", evidence=[]),  # rests on nothing: not offered
                investigate._Option(title="Restart the router", why="It helps.", how="Unplug it", evidence=["F9"]),  # cites a finding that does not exist: not offered
            ])
        raise AssertionError(schema)

    async def write(self, system, user):
        return ""

    async def search(self, query, count):
        return [{"title": "Cast", "url": "https://help.example/cast", "snippet": ""}]

    async def fetch(self, url):
        return Page("Cast help", PAGE)

    async def probe(self, op, args):
        self.ran.append((op, dict(args)))
        return Probe(op, True, {"network_overview": NETWORK, "lan_devices": DEVICES, "host_check": "192.168.1.224: answers a ping. Open ports: 8008, 8009."}.get(op, f"{op}: fine"))

    @property
    def io(self) -> ResearchIO:
        return ResearchIO(think=self.think, write=self.write, search=self.search, fetch=self.fetch, probe=self.probe)


async def _run(fake: _Fake, goal: str = "My TV will not take a cast from this computer"):
    state, said, report = investigate.new_state(), [], None
    for _ in range(60):
        unit = await investigate.advance(goal, state, fake.io)
        state = unit.state
        if unit.say:
            said.append(unit.say)
        if unit.report is not None:
            report = unit.report
            break
    return state, said, report


@pytest.mark.asyncio
async def test_it_looks_first_keeps_only_what_a_result_shows_and_offers_only_what_is_supported() -> None:
    fake = _Fake()
    state, said, report = await _run(fake)

    assert [op for op, _args in fake.ran] == ["network_overview", "lan_devices", "host_check"]  # the plan's duplicates and the check that does not exist are dropped; one more check on the TV's address
    assert fake.ran[2][1] == {"host": "192.168.1.224"}
    assert [item["text"] for item in state["findings"]] == ["Ethernet is on a Public network.", "Two devices answer on 192.168.1.."] or len(state["findings"]) == 2  # a finding that names a check that did not run is dropped
    assert all(finding["probe"] in {"network_overview", "lan_devices"} for finding in state["findings"])
    assert {item["id"]: item["status"] for item in state["hypotheses"]} == {"H1": "supported", "H2": "refuted", "H3": "open"}  # H9 was not a listed cause
    assert state["web_notes"] and state["web_notes"][0]["claim"] == "Discovery is off on a Public network." and state["web_notes"][0]["url"] == "https://help.example/cast"
    assert [option["title"] for option in state["options"]] == ["Set the network to Private"]  # the two that rested on nothing are not offered
    assert state["options"][0]["evidence"] == ["F1", "W1"] and state["options"][0]["changes_something"]
    assert said[0].startswith("I will look into this properly: first I check network overview, lan devices")
    assert state["stage"] == "done" and "Ways forward:" in state["summary"] and "1. Set the network to Private" in state["summary"] and "I will ask you first" in state["summary"]
    assert "To undo it: Set it back to Public" in state["summary"] and "What I could not tell: A firewall blocks discovery" in state["summary"]
    assert report.startswith("# My TV will not take a cast") and "## What each check showed" in report and "### host check {'host': '192.168.1.224'}" in report and "[W1]" in report


@pytest.mark.asyncio
async def test_a_check_is_only_pointed_at_an_address_the_results_showed() -> None:
    fake = _Fake(extra_probe={"host": "8.8.8.8"})  # the model asks for a check on an address nothing showed
    state, _said, _report = await _run(fake)
    assert [op for op, _args in fake.ran] == ["network_overview", "lan_devices"] and state["stage"] == "done"
    assert not is_private("8.8.8.8") and is_private("192.168.1.224") and is_private("printer.local") and not is_private("example.com")


@pytest.mark.asyncio
async def test_with_nothing_supported_it_says_so_and_asks_for_more_instead_of_inventing_a_way_forward() -> None:
    class _Blank(_Fake):
        async def think(self, system, user, schema):
            if schema is investigate._Options:
                return investigate._Options(options=[investigate._Option(title="Try something", why="Maybe", how="Do it", evidence=[])])
            if schema is investigate._Analysis:
                return investigate._Analysis(enough=True)
            return await super().think(system, user, schema)

        async def search(self, query, count):
            raise RuntimeError("offline")

    state, _said, _report = await _run(_Blank())
    assert state["options"] == [] and "The checks did not settle anything." in state["summary"] and "no way forward that the evidence supports" in state["summary"]


@pytest.mark.asyncio
async def test_the_engine_needs_the_checks_and_progress_is_told_in_words() -> None:
    with pytest.raises(RuntimeError, match="not available"):
        await investigate.advance("x", investigate.new_state(), ResearchIO(think=None, write=None, search=None, fetch=None))
    assert investigate.progress_text({"stage": "baseline", "observations": [1], "queue": [1, 2]}) == "Checking this computer and the network: 1 checks done, 2 to go."
    assert engines.engine_for("investigate") is investigate and engines.engine_for("research") is research


def test_the_parsers_read_what_devices_say() -> None:
    reply = b"HTTP/1.1 200 OK\r\nST: urn:dial-multiscreen-org:service:dial:1\r\nSERVER: Linux UPnP/1.0 Chromecast\r\nLOCATION: http://192.168.1.72:8008/ssdp/device-desc.xml\r\n\r\n"
    assert parse_ssdp(reply)["LOCATION"] == "http://192.168.1.72:8008/ssdp/device-desc.xml" and parse_ssdp(reply)["ST"].endswith("dial:1")
    query = diagnose._mdns_query("_googlecast._tcp.local")
    assert query.endswith(b"\x00\x00\x0c\x80\x01") and b"\x0b_googlecast\x04_tcp\x05local" in query
    answer = (
        b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00" + b"\x0b_googlecast\x04_tcp\x05local\x00" + b"\x00\x0c\x00\x01\x00\x00\x00\x78" + b"\x00\x1a"
        + b"\x17Chromecast-0123456789ab\xc0\x0c"
    )
    names = parse_mdns(answer)
    assert "_googlecast._tcp.local" in names and any(name.startswith("Chromecast-0123456789ab._googlecast") for name in names)
    assert parse_mdns(b"junk") == []


@pytest.mark.asyncio
async def test_an_unknown_check_is_a_failed_check_with_the_menu_not_an_exception() -> None:
    result = await diagnose.run_probe("format_the_disk")
    assert not result.ok and "network_overview" in result.summary


@pytest.mark.asyncio
async def test_a_finished_investigation_gives_its_goal_what_it_found_and_waits_for_the_persons_pick(tmp_path) -> None:
    repos, audit = make_repos(tmp_path)
    goal = repos.goals.create(Goal(conversation_id="c1", objective="Get the TV to take casts", status=GoalStatus.OPEN))
    fake = _Fake()
    sent = []

    async def say(routing, text):
        sent.append(text)

    async def send_file(routing, path, caption):
        sent.append(path)

    job = repos.jobs.create(Job(kind="investigate", goal="My TV will not take a cast", state=investigate.new_state(), routing={"source_chat_id": "42", "goal_id": goal.id}))
    runner = JobRunner(repos, audit, lambda _job: fake.io, say=say, send_file=send_file, reports_dir=tmp_path / "jobs")
    for _ in range(80):
        await runner.tick()
        if repos.jobs.get(job.id).status == JobStatus.DONE:
            break
    assert repos.jobs.get(job.id).status == JobStatus.DONE
    assert any(text.startswith("The investigation is done.") and "Ways forward:" in text for text in sent) and any(str(text).endswith("report.docx") for text in sent)
    saved = repos.goals.get(goal.id)
    assert saved.status == GoalStatus.WAITING and saved.waiting == {"question": "Which way forward should I try?"}
    assert any(line.startswith("Found: Ethernet is on a Public network") for line in saved.evidence) and any(line.startswith("Option 1: Set the network to Private") for line in saved.evidence)
    assert saved.attempts[-1]["kind"] == "job" and saved.attempts[-1]["outcome"] == "investigated"
