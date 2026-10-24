"""How a message written during a task is sent on: the person's words first, then a judge, then the safe default."""
from harness.router import QUEUE, STEER, URGENT, Router, judge_with_model


def test_clear_words_say_to_wait_or_to_hurry_in_italian_and_english():
    r = Router()
    for text in ("Quando hai finito controlla le mail", "dopo che hai finito, scrivi a Gianni", "when you are done, check the mail", "no rush, later is fine", "non è urgente"):
        assert r.route(text, {}) == (QUEUE, "words"), text
    for text in ("È urgente: chiama il medico", "Prima di tutto telefona a Lucia", "urgent! call the plumber", "do this asap"):
        assert r.route(text, {}) == (URGENT, "words"), text


def test_a_weak_word_of_hurry_alone_does_not_make_a_request_unrelated():
    r = Router()
    assert r.route("scrivi subito il file", {}) == (STEER, "default")                 # "write it right now" is about the task
    assert r.route("do it right now", {}) == (STEER, "default")
    assert Router(lambda t, c: "queue").route("scrivi subito il file", {}) == (URGENT, "judge")   # unless the judge says it is another request
    assert Router(lambda t, c: "steer").route("scrivi subito il file", {}) == (STEER, "judge")


def test_a_strong_word_of_hurry_is_overruled_only_by_a_judge_who_says_it_is_about_the_task():
    assert Router(lambda t, c: "steer").route("urgente: cambia il titolo", {}) == (STEER, "judge")
    assert Router(lambda t, c: "queue").route("urgente: chiama Gianni", {}) == (URGENT, "words")


def test_without_words_the_judge_decides_and_without_a_judge_the_message_goes_into_the_task():
    assert Router(lambda t, c: "queue").route("controlla le mail", {}) == (QUEUE, "judge")
    assert Router(lambda t, c: "urgent").route("la caldaia perde", {}) == (URGENT, "judge")
    assert Router(lambda t, c: None).route("aggiungi il caffè", {}) == (STEER, "default")      # a judge that cannot tell
    assert Router(lambda t, c: "boh").route("aggiungi il caffè", {}) == (STEER, "default")     # or says something else
    assert Router().route("aggiungi il caffè", {}) == (STEER, "default")


def test_the_model_judge_asks_for_one_word_and_gives_up_quietly():
    class Client:
        def __init__(self, reply):
            self.reply, self.asked = reply, []

        def chat_completion(self, messages, **kw):
            self.asked.append((messages, kw))
            if isinstance(self.reply, Exception):
                raise self.reply
            return {"choices": [{"message": {"content": self.reply}}]}

    c = Client("Urgent.")
    assert judge_with_model(c)("la caldaia perde", {"task": "organizza la cena", "progress": "ho letto il piano"}) == "urgent"
    system, user = c.asked[0][0]
    assert "steer" in system["content"] and "organizza la cena" in user["content"] and "la caldaia perde" in user["content"]
    assert c.asked[0][1]["max_tokens"] <= 8 and c.asked[0][1]["temperature"] == 0.0
    assert judge_with_model(Client("queue, because it can wait"))("x", {}) == "queue"
    assert judge_with_model(Client("I think so"))("x", {}) is None
    assert judge_with_model(Client(RuntimeError("down")))("x", {}) is None
