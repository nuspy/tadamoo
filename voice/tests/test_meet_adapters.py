"""The platform adapters against a fake DOM: a page whose elements are found when their key text appears in the selector asked for. No browser."""
import pytest

from callbridge.meetbot.adapters import MeetAdapter, TeamsAdapter, ZoomAdapter, adapter_for


class El:
    def __init__(self, key, text="", attrs=None, visible=True):
        self.key, self.text, self.attrs, self.visible = key, text, attrs or {}, visible
        self.clicked = 0
        self.value = None
        self.pressed = []
        self.flips = []                                      # elements that appear or disappear when this one is clicked


class Loc:
    def __init__(self, page, sel, index=0):
        self.page, self.sel, self.index = page, sel, index

    @property
    def _all(self):
        return [e for e in self.page.elements if e.key in self.sel]

    @property
    def first(self):
        return Loc(self.page, self.sel, 0)

    def nth(self, i):
        return Loc(self.page, self.sel, i)

    def count(self):
        return len(self._all)

    @property
    def _el(self):
        found = self._all
        if self.index >= len(found):
            raise RuntimeError("no such element: " + self.sel[:40])
        return found[self.index]

    def is_visible(self, timeout=None):
        found = self._all
        return self.index < len(found) and found[self.index].visible

    def wait_for(self, state="visible", timeout=None):
        if not self.is_visible():
            raise TimeoutError("not visible")

    def click(self, timeout=None):
        el = self._el
        if not el.visible:
            raise TimeoutError("not visible")
        el.clicked += 1
        self.page.clicks.append(el.key)
        for other in el.flips:
            other.visible = not other.visible
        if "aria-pressed" in el.attrs:
            el.attrs["aria-pressed"] = "false" if el.attrs["aria-pressed"] == "true" else "true"

    def fill(self, text):
        self._el.value = text

    def press(self, key):
        self._el.pressed.append(key)

    def get_attribute(self, name):
        return self._el.attrs.get(name)

    def inner_text(self, timeout=None):
        return self._el.text


class Page:
    def __init__(self, elements, url="https://meet.google.com/abc-defg-hij"):
        self.elements, self.url, self.clicks, self.gotos = elements, url, [], []

    def locator(self, sel):
        return Loc(self, sel)

    def wait_for_load_state(self, *a, **kw):
        pass

    def goto(self, url, timeout=None):
        self.gotos.append(url)
        self.url = url


def by_key(page, key):
    return next(e for e in page.elements if e.key == key)


# ---- routing
def test_the_three_platforms_are_routed_and_the_others_are_not_built():
    assert isinstance(adapter_for("https://meet.google.com/abc-defg-hij"), MeetAdapter)
    assert isinstance(adapter_for("https://teams.microsoft.com/l/meetup-join/19%3a"), TeamsAdapter)
    assert isinstance(adapter_for("https://teams.live.com/meet/9"), TeamsAdapter)
    assert isinstance(adapter_for("https://us02web.zoom.us/j/123456789?pwd=abc"), ZoomAdapter)
    for other in ("https://acme.webex.com/meet/x", "https://app.slack.com/huddle/T/C", "https://t.me/+abc", "https://example.com/"):
        assert adapter_for(other) is None


def test_only_the_platforms_own_hosts_over_https_are_joined():
    """The bot opens a visible browser at the address it is given: a look-alike address must not reach an adapter."""
    for spoof in ("https://evil.example/meet.google.com/abc-defg-hij", "https://meet.google.com.evil.example/abc", "https://evil.example/?u=https://meet.google.com/x",
                  "http://meet.google.com/abc-defg-hij", "https://meet.google.com/", "https://evilmeet.google.com/abc", "https://teams.microsoft.com.evil.example/l/meetup-join/x",
                  "https://evil.example/zoom.us/j/1", "https://notzoom.us/j/1", "https://zoom.us.evil.example/j/1", "https://us02web.zoom.us/about", "file:///c:/x"):
        assert adapter_for(spoof) is None, spoof


# ---- Google Meet
def meet_lobby(speaking=False):
    return Page([El('placeholder*="name"'), El("Turn off microphone"), El("Turn off camera"), El("Join now"),
                 El("Leave call", visible=speaking)])


def test_meet_join_as_a_guest_with_microphone_and_camera_off():
    """Always off at the door: the device a meeting picks may be the person's own microphone or webcam."""
    page = meet_lobby(speaking=True)
    res = MeetAdapter().join(page, "TaDaMoo Bot")
    assert res.ok and res.state == "in_call"
    assert by_key(page, 'placeholder*="name"').value == "TaDaMoo Bot"
    assert by_key(page, "Turn off microphone").clicked == 1 and by_key(page, "Turn off camera").clicked == 1 and by_key(page, "Join now").clicked == 1


def test_the_microphone_and_the_camera_are_switched_on_and_off_in_a_meeting():
    mic_off, mic_on = El("Turn off microphone", visible=False), El("Turn on microphone", visible=True)
    mic_on.flips, mic_off.flips = [mic_off, mic_on], [mic_off, mic_on]
    cam_off, cam_on = El("Turn off camera", visible=False), El("Turn on camera", visible=True)
    cam_on.flips, cam_off.flips = [cam_off, cam_on], [cam_off, cam_on]
    page = Page([mic_off, mic_on, cam_off, cam_on])
    a = MeetAdapter()
    assert a.set_mic(page, False) is True and mic_on.clicked == 0                     # already off: nothing is clicked
    assert a.set_mic(page, True) is True and mic_on.clicked == 1 and mic_off.visible
    assert a.set_mic(page, True) is True and mic_on.clicked == 1
    assert a.set_mic(page, False) is True and mic_off.clicked == 1 and mic_on.visible
    assert a.set_cam(page, True) is True and a.set_cam(page, False) is True
    assert a.set_mic(Page([]), True) is False and a.set_cam(Page([]), True) is False   # no such control: it says so


def test_teams_and_zoom_switch_the_microphone_too():
    toggle = El("toggle-mute", attrs={"aria-pressed": "false"})
    teams = Page([toggle, El("toggle-video", attrs={"aria-pressed": "false"})])
    assert TeamsAdapter().set_mic(teams, True) is True and toggle.attrs["aria-pressed"] == "true"
    assert TeamsAdapter().set_mic(teams, True) is True and toggle.clicked == 1
    assert TeamsAdapter().set_mic(teams, False) is True and toggle.attrs["aria-pressed"] == "false"
    unmute, mute = El('[aria-label^="unmute"', visible=True), El('[aria-label^="mute"', visible=False)
    unmute.flips, mute.flips = [unmute, mute], [unmute, mute]
    zoom = Page([unmute, mute])
    assert ZoomAdapter().set_mic(zoom, True) is True and mute.visible and ZoomAdapter().set_mic(zoom, False) is True and unmute.visible


def test_meet_join_in_the_waiting_room_and_when_it_cannot():
    res = MeetAdapter().join(meet_lobby(speaking=False), "B")
    assert res.ok and res.state == "waiting_admit"
    res = MeetAdapter().join(Page([El('placeholder*="name"')]), "B")
    assert not res.ok and res.state == "error" and res.message


def test_meet_in_call_and_leave():
    page = Page([El("Leave call")])
    a = MeetAdapter()
    assert a.in_call(page)
    a.leave(page)
    assert by_key(page, "Leave call").clicked == 1
    assert not a.in_call(Page([]))
    a.leave(Page([]))                                          # leaving a page without the button does not raise


def test_meet_participants_and_the_active_speaker():
    page = Page([El("[data-participant-id]", attrs={"aria-label": "Ada Lovelace, host"}), El("[data-participant-id]", attrs={"aria-label": "Bruno", "class": "x speaking"}),
                 El("[data-participant-id]", attrs={"aria-label": "Ada Lovelace, host"})])
    info = MeetAdapter().speakers(page)
    assert info.participants == ["Ada Lovelace", "Bruno"] and info.active == "Bruno"
    assert MeetAdapter().speakers(Page([])).participants == []


def test_meet_chat_post_and_read():
    page = Page([El("Chat with everyone"), El("Send a message", visible=True),
                 El("[data-message-id]", "hello", {"data-sender-name": "Ada"}), El("[data-message-id]", "stop", {"data-sender-name": "Bruno"})])
    a = MeetAdapter()
    assert a.send_chat(page, "I am an AI assistant")
    assert by_key(page, "Send a message").value == "I am an AI assistant" and by_key(page, "Send a message").clicked == 1
    msgs = a.read_chat(page)
    assert [(m.sender, m.text) for m in msgs] == [("Ada", "hello"), ("Bruno", "stop")]
    assert a.read_chat(page) == []                               # only what is new
    page.elements.append(El("[data-message-id]", "later", {"data-sender-name": "Carla"}))
    assert [m.text for m in a.read_chat(page)] == ["later"]


def test_meet_chat_opens_when_closed_and_fails_plainly_when_there_is_none():
    page = Page([El("Chat with everyone"), El("Send a message", visible=False)])
    a = MeetAdapter()
    assert a.send_chat(page, "x") is False                      # the box never shows: not posted, no exception
    assert MeetAdapter().send_chat(Page([]), "x") is False
    assert MeetAdapter().read_chat(Page([])) == []


def test_exit_reason_tells_removal_from_the_end_of_the_meeting():
    a = MeetAdapter()
    assert a.exit_reason(Page([El("body", "You've been removed from the meeting")])) == "removed"
    assert a.exit_reason(Page([El("body", "Sei stato rimosso dalla riunione")])) == "removed"
    assert a.exit_reason(Page([El("body", "The meeting has ended for everyone")])) == "ended"
    assert a.exit_reason(Page([El("body", "La riunione è terminata")])) == "ended"
    assert a.exit_reason(Page([El("body", "Return to home screen")])) == "ended"
    assert a.exit_reason(Page([El("body", "something else")])) == ""
    assert a.exit_reason(Page([])) == ""


# ---- Teams
def test_teams_join_goes_through_the_web_client_as_a_guest():
    page = Page([El("Continue on this browser"), El("prejoin-display-name-input"), El("toggle-mute", attrs={"aria-pressed": "true"}),
                 El("toggle-video", attrs={"aria-pressed": "true"}), El("prejoin-join-button"), El("hangup-main-btn")], url="https://teams.microsoft.com/l/meetup-join/x")
    res = TeamsAdapter().join(page, "TaDaMoo Bot")
    assert res.ok and res.state == "in_call"
    assert by_key(page, "Continue on this browser").clicked == 1 and by_key(page, "prejoin-display-name-input").value == "TaDaMoo Bot"
    assert by_key(page, "toggle-mute").clicked == 1 and by_key(page, "toggle-video").clicked == 1 and by_key(page, "prejoin-join-button").clicked == 1


def test_teams_waiting_in_the_lobby_roster_chat_and_removal():
    page = Page([El("prejoin-join-button")])
    assert TeamsAdapter().join(page, "B").state == "waiting_admit"
    page = Page([El("hangup-main-btn"), El('data-tid="participantsInCall"', attrs={"aria-label": "Ada, organizer"}), El("chat-button"), El("ckeditor"),
                 El("newMessageCommands-send"), El("chat-pane-message", "hi", {"data-sender-name": "Ada"})])
    a = TeamsAdapter()
    assert a.in_call(page) and a.speakers(page).participants == ["Ada"]
    assert a.send_chat(page, "hello") and by_key(page, "ckeditor").value == "hello"
    assert [m.text for m in a.read_chat(page)] == ["hi"]
    assert a.exit_reason(Page([El("body", "You were removed from this meeting")])) == "removed"


# ---- Zoom
def test_zoom_goes_to_the_browser_client_and_joins():
    assert ZoomAdapter.web_client("https://us02web.zoom.us/j/123456789?pwd=abc.def") == "https://app.zoom.us/wc/123456789/join?pwd=abc.def"
    assert ZoomAdapter.web_client("https://zoom.us/j/55") == "https://app.zoom.us/wc/55/join"
    page = Page([El("#input-for-name"), El('button:has-text("Join")'), El("Leave")], url="https://us02web.zoom.us/j/123456789?pwd=abc")
    res = ZoomAdapter().join(page, "TaDaMoo Bot")
    assert res.ok and res.state == "in_call" and page.gotos == ["https://app.zoom.us/wc/123456789/join?pwd=abc"]
    assert by_key(page, "#input-for-name").value == "TaDaMoo Bot"


def test_zoom_participants_chat_leave_and_removal():
    page = Page([El("Leave"), El("participants-item__display-name", "Ada"), El("participants-item__display-name", "Bruno"), El('[aria-label*="chat"'),
                 El("chat-box__chat-textarea"), El("chat-message__container", "stop")])
    a = ZoomAdapter()
    assert a.speakers(page).participants == ["Ada", "Bruno"]
    assert a.send_chat(page, "hi") and by_key(page, "chat-box__chat-textarea").pressed == ["Enter"]     # no send button: Enter sends
    assert [m.text for m in a.read_chat(page)] == ["stop"]
    a.leave(page)
    assert by_key(page, "Leave").clicked >= 1                     # the leave button, then its confirmation (the fake finds both under one key)
    assert a.exit_reason(Page([El("body", "You have been removed by the host")])) == "removed"
    assert a.exit_reason(Page([El("body", "This meeting has been ended by host")])) == "ended"


@pytest.mark.parametrize("cls", [MeetAdapter, TeamsAdapter, ZoomAdapter])
def test_every_adapter_keeps_its_selectors_in_class_constants(cls):
    constants = [n for n in vars(cls) if n.startswith("SEL_")]
    assert "SEL_LEAVE_BTN" in constants and "SEL_JOIN_BTN" in constants and "SEL_CHAT_INPUT" in constants
