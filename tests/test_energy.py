"""Energy of a task from power samples: the sum over time, the limit, and nothing without readings."""
import time

from harness.energy import EnergyMeter
from harness.scheduler import Scheduler


def gpus(*watts, limit=450.0):
    return [{"index": i, "power": w, "power_limit": limit} for i, w in enumerate(watts)]


def test_energy_is_the_area_under_the_power_curve():
    readings = iter([gpus(100.0), gpus(300.0), []])                        # the last one is the sample stop() takes: none comes
    meter = EnergyMeter(lambda: next(readings))
    meter.sample(0.0)
    meter.sample(10.0)
    res = meter.stop()
    assert res["energy_wh"] == round(200 * 10 / 3600, 3)                  # a mean of 200 W over 10 s
    assert res["avg_power_w"] == 200.0 and res["peak_power_w"] == 300.0
    assert res["gpu_power_limit_w"] == 450.0 and res["samples"] == 2 and res["gpus"] == 1 and "whole GPU" in res["note"]


def test_two_gpus_are_added_together():
    readings = iter([gpus(100.0, 50.0, limit=300.0)] * 2 + [[]])
    meter = EnergyMeter(lambda: next(readings))
    meter.sample(0.0)
    meter.sample(3600.0)
    res = meter.stop()
    assert res["energy_wh"] == 150.0 and res["gpu_power_limit_w"] == 600.0 and res["gpus"] == 2


def test_no_readings_means_no_figures():
    assert EnergyMeter(lambda: []).start().stop() == {}
    assert EnergyMeter(lambda: 1 / 0).start().stop() == {}
    assert EnergyMeter(lambda: [{"power": None, "power_limit": None}]).start().stop() == {}


def test_only_the_limit_is_reported_when_power_cannot_be_read():
    res = EnergyMeter(lambda: [{"power": None, "power_limit": 575.0}]).start().stop()
    assert res == {"gpu_power_limit_w": 575.0, "note": res["note"]}


def test_the_sampler_thread_collects_samples_while_the_task_runs():
    meter = EnergyMeter(lambda: gpus(200.0), interval=0.02).start()
    time.sleep(0.3)
    res = meter.stop()
    assert res["samples"] >= 5 and res["avg_power_w"] == 200.0 and res["energy_wh"] > 0


def test_a_routine_result_carries_the_energy(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()

    class Runner:
        def run(self, routine, cancel):
            time.sleep(0.15)
            return {"status": "done", "summary": "ok"}

    sched = Scheduler(tmp_path / "d", Runner(), tick_s=3600, poll_s=0.02, meter=lambda: EnergyMeter(lambda: gpus(250.0), interval=0.02))
    rid = sched.add("n", "p", str(folder), "every 1h")["id"]
    sched.start()
    sched.run_now(rid)
    end = time.monotonic() + 10
    while not sched.runs() and time.monotonic() < end:
        time.sleep(0.02)
    run = sched.runs()[0]
    assert run["status"] == "done" and run["energy"]["avg_power_w"] == 250.0 and run["energy"]["gpu_power_limit_w"] == 450.0
    sched.shutdown()
