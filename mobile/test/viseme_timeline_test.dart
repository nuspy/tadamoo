import 'package:basalt_mobile/src/playback_tracker.dart';
import 'package:basalt_mobile/src/viseme_timeline.dart';
import 'package:flutter_test/flutter_test.dart';

VisemeSegment segment(int start, {int id = 1, int duration = 1000, List<List<Object>>? events}) => VisemeSegment(
      id,
      start,
      duration,
      [for (final e in events ?? [[0, 100, 'sil'], [100, 200, 'aa'], [300, 200, 'PP'], [500, 500, 'O']]) VisemeEvent(e[0] as int, e[1] as int, e[2] as String)],
    );

void main() {
  group('the shape at a time of the audio clock', () {
    test('it is the event that covers the time, with the picture drawn a little ahead of the sound', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(1000));
      expect(t.at(1050).v, 'sil');
      expect(t.at(1150).v, 'aa');
      expect(t.at(1350).v, 'PP');
      expect(t.at(1700).v, 'O');
      final ahead = VisemeTimeline(leadMs: 40, fadeMs: 0)..add(segment(1000));
      expect(ahead.at(1070).v, 'aa', reason: 'the frame for 1110 ms is shown at 1070');
    });

    test('a shape is held until the next one starts, even across a gap in the events', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(0, events: [[0, 100, 'FF'], [400, 100, 'kk']]));
      expect(t.at(250).v, 'FF');
      expect(t.at(450).v, 'kk');
    });

    test('the mouth rests where no sentence is: before the first, between two and after the last', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(1000, id: 1, duration: 500, events: [[0, 500, 'aa']]));
      t.add(segment(2000, id: 2, duration: 500, events: [[0, 500, 'E']]));
      expect(t.at(500).v, 'sil');
      expect(t.at(1200).v, 'aa');
      expect(t.at(1700).v, 'sil');
      expect(t.at(2200).v, 'E');
      expect(t.at(2600).v, 'sil');
    });

    test('a sentence that comes right after another continues on the same clock', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(0, id: 1, duration: 500, events: [[0, 500, 'aa']]));
      t.add(segment(500, id: 2, duration: 500, events: [[0, 500, 'U']]));
      expect(t.at(499).v, 'aa');
      expect(t.at(500).v, 'U');
    });

    test('a change of shape fades over the time asked, from the one before', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 50);
      t.add(segment(0, events: [[0, 200, 'sil'], [200, 200, 'aa']]));
      expect(t.at(100).v, 'sil');
      final start = t.at(200);
      expect((start.v, start.prev, start.mix), ('aa', 'sil', 0.0));
      expect(t.at(225).mix, closeTo(0.5, 1e-9));
      expect(t.at(300).mix, 1.0);
    });

    test('flush forgets every sentence at once and the mouth is at rest', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(0));
      t.add(segment(1000, id: 2));
      expect(t.at(150).v, 'aa');
      expect(t.segmentCount, 2);
      t.flush();
      expect(t.segmentCount, 0);
      expect(t.at(150).v, 'sil');
      expect(t.at(1150).v, 'sil');
    });

    test('a sentence long past is dropped', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(0));
      t.at(10000);
      expect(t.segmentCount, 0);
    });

    test('a timeline that arrives again for the same sentence replaces the first', () {
      final t = VisemeTimeline(leadMs: 0, fadeMs: 0);
      t.add(segment(0, id: 3, events: [[0, 1000, 'aa']]));
      t.add(segment(0, id: 3, events: [[0, 1000, 'E']]));
      expect(t.segmentCount, 1);
      expect(t.at(100).v, 'E');
    });
  });

  group('the PC\'s visemes message', () {
    test('it becomes a segment placed where the audio of the earlier sentences ends', () {
      final s = VisemeSegment.fromMessage({
        'type': 'visemes',
        'segment': 2,
        'duration_ms': 1840,
        'pcm_bytes': 88320,
        'visemes': [
          {'t': 120, 'v': 'CH', 'dur': 210},
          {'t': 0, 'v': 'sil', 'dur': 120},
        ],
      }, 4200)!;
      expect((s.id, s.startMs, s.durationMs, s.endMs), (2, 4200, 1840, 6040));
      expect([for (final e in s.events) e.v], ['sil', 'CH'], reason: 'sorted by time');
    });

    test('the duration comes from the bytes when it is not there, or from the last event', () {
      expect(VisemeSegment.fromMessage({'pcm_bytes': 48000, 'visemes': []}, 0, sampleRate: 24000)!.durationMs, 1000);
      expect(VisemeSegment.fromMessage({'visemes': [{'t': 100, 'v': 'aa', 'dur': 300}]}, 0)!.durationMs, 400);
      expect(VisemeSegment.fromMessage({'visemes': []}, 0), isNull);
    });

    test('an event that is not usable is skipped and the rest stands', () {
      final s = VisemeSegment.fromMessage({
        'duration_ms': 500,
        'visemes': [
          {'t': 0, 'v': 'aa', 'dur': 100},
          {'t': 'x', 'v': 'aa', 'dur': 100},
          {'t': 100, 'v': 'ZZ', 'dur': 100},
          {'t': 200, 'dur': 100},
          'junk',
          {'t': 300, 'v': 'O', 'dur': 100},
        ],
      }, 0)!;
      expect([for (final e in s.events) e.v], ['aa', 'O']);
    });
  });

  group('the idle motion', () {
    test('with reduced motion the picture does not move at all', () {
      const m = IdleMotion(reduced: true);
      for (final ms in [0, 1234, 99999]) {
        expect(m.at(ms), (0.0, 0.0, 0.0, 1.0));
      }
    });

    test('otherwise it is slight and changes with time', () {
      const m = IdleMotion();
      final a = m.at(1000);
      final b = m.at(3000);
      expect(a, isNot(b));
      for (final v in [a, b, m.at(5000)]) {
        expect(v.$1.abs(), lessThan(0.01));
        expect(v.$2.abs(), lessThan(0.01));
        expect(v.$3.abs(), lessThan(0.02));
        expect(v.$4, inInclusiveRange(0.99, 1.01));
      }
    });
  });

  group('the playback clock', () {
    test('without reports it runs from the time the audio was handed over, after the delay assumed', () {
      var now = 1000;
      final t = PlaybackTracker(() => now, latencyMs: 100);
      t.added(48000, 24000);
      expect(t.queuedMs, 1000);
      expect(t.playedMs, 0);
      now = 1100;
      expect(t.playedMs, 0);
      now = 1600;
      expect(t.playedMs, 500);
      now = 5000;
      expect(t.playedMs, 1000, reason: 'never beyond what was handed over');
      expect(t.drained, isTrue);
    });

    test('audio handed over after a silence starts again from there', () {
      var now = 0;
      final t = PlaybackTracker(() => now, latencyMs: 0);
      t.added(48000, 24000);
      now = 3000;
      expect(t.drained, isTrue);
      t.added(24000, 24000);
      expect(t.queuedMs, 1500);
      now = 3250;
      expect(t.playedMs, 1250);
    });

    test('a report of the player\'s own position takes over, and the estimate follows it', () {
      var now = 0;
      final t = PlaybackTracker(() => now, latencyMs: 0);
      t.added(96000, 24000);
      now = 500;
      t.report(300);
      expect(t.playedMs, 300);
      now = 700;
      expect(t.playedMs, 500);
      t.added(48000, 24000);
      now = 5000;
      expect(t.playedMs, 3000, reason: 'a track that was reported is not re-anchored by the wall clock');
    });

    test('reset starts both clocks again', () {
      var now = 0;
      final t = PlaybackTracker(() => now, latencyMs: 0);
      t.added(48000, 24000);
      t.reset();
      expect((t.queuedMs, t.playedMs), (0, 0));
    });
  });
}
