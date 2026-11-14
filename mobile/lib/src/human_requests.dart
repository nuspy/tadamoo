import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'api.dart';
import 'i18n.dart';

/// A choice of a question: `value` is what the plan gets and is never shown; the label is what the person reads, in the app's language.
class HumanChoice {
  const HumanChoice(this.value, this.label, this.labelEn);
  final String value, label, labelEn;
  String get shown => Texts.language == 'en' ? (labelEn.isEmpty ? label : labelEn) : (label.isEmpty ? labelEn : label);
}

/// What a plan asks the person: to approve what it shows (yes or no), to give a value, or to do a task (done, with a note, or not now).
/// The PC lists these for all the plans (`/api/plans/human`).
class HumanRequest {
  const HumanRequest({
    required this.planId,
    required this.planTitle,
    required this.nodeId,
    required this.title,
    required this.ask,
    required this.prompt,
    this.promptEn = '',
    this.help = '',
    this.helpEn = '',
    this.content = '',
    this.kind = 'text',
    this.choices = const [],
    this.other = false,
    this.index = 0,
    this.proposed,
  });

  factory HumanRequest.fromJson(Map<String, dynamic> j) => HumanRequest(
        planId: '${j['plan_id'] ?? ''}',
        planTitle: '${j['plan_title'] ?? ''}',
        nodeId: '${j['node_id'] ?? ''}',
        title: '${j['title'] ?? ''}',
        ask: '${j['ask'] ?? 'value'}',
        prompt: '${j['prompt'] ?? ''}',
        promptEn: '${j['prompt_en'] ?? ''}',
        help: '${j['help'] ?? ''}',
        helpEn: '${j['help_en'] ?? ''}',
        content: '${j['content'] ?? ''}',
        kind: '${j['kind'] ?? 'text'}',
        other: j['other'] == true,
        index: (j['index'] as num?)?.toInt() ?? 0,
        proposed: j['proposed'],
        choices: [
          for (final c in (j['choices'] as List?) ?? const [])
            if (c is Map) HumanChoice('${c['value']}', '${c['label'] ?? c['value']}', '${c['label_en'] ?? c['label'] ?? c['value']}') else HumanChoice('$c', '$c', '$c'),
        ],
      );

  final String planId, planTitle, nodeId, title, ask, prompt, promptEn, help, helpEn, content, kind;
  final List<HumanChoice> choices;
  final bool other;
  final int index;

  /// What the plan proposes, or what was answered the last time.
  final Object? proposed;
  bool get approve => ask == 'approve';
  bool get task => ask == 'task';

  /// Two possible answers (yes or no, or two choices) are two buttons.
  bool get twoWays => approve || (!task && kind == 'choice' && choices.length == 2);

  String get question => Texts.language == 'en' ? (promptEn.isEmpty ? prompt : promptEn) : (prompt.isEmpty ? promptEn : prompt);
  String get explanation => Texts.language == 'en' ? (helpEn.isEmpty ? help : helpEn) : (help.isEmpty ? helpEn : help);
}

/// The requests of one plan that are asking now, in the order of the plan: one path of questions.
class HumanGroup {
  HumanGroup(this.planId, this.planTitle, this.items);
  final String planId, planTitle;
  final List<HumanRequest> items;
  String get ids => items.map((r) => r.nodeId).join('|');

  /// The first plan that has requests asking that `skip` does not hold back (a plan, or a task, left for later), with those requests; or null.
  static HumanGroup? first(List<HumanRequest> all, bool Function(HumanRequest r) skip) {
    final rows = [for (final r in all) if (!skip(r)) r];
    if (rows.isEmpty) return null;
    final id = rows.first.planId;
    final items = [for (final x in rows) if (x.planId == id) x]..sort((a, b) => a.index.compareTo(b.index));
    return HumanGroup(id, items.first.planTitle, items);
  }
}

/// The path through the questions: step i of n is a question, step n is the summary. `answers` start with what the plan proposes. The answer to a task is
/// `{'state': 'done' | 'later', 'note': text}`.
class HumanPath {
  HumanPath(this.items, [Map<String, Object?>? before])
      : answers = {
          for (final r in items)
            if (before != null && before.containsKey(r.nodeId)) r.nodeId: before[r.nodeId] else if (r.proposed != null) r.nodeId: r.proposed,
        } {
    for (final r in items) {
      if (writing(r, answers[r.nodeId])) other.add(r.nodeId);
    }
  }
  final List<HumanRequest> items;
  final Map<String, Object?> answers;

  /// The questions of more than two choices whose answer is being written (the menu's "Other, I write").
  final Set<String> other = {};
  int step = 0;
  bool fromSummary = false;

  bool get inSummary => step >= items.length;

  /// A menu of more than two choices that offers Other, with an answer that is not one of the choices.
  static bool writing(HumanRequest r, Object? v) => r.other && v != null && '$v'.isNotEmpty && !r.choices.any((c) => c.value == '$v');

  Map<String, Object?> _task(HumanRequest r) {
    final v = answers[r.nodeId];
    return v is Map<String, Object?> ? v : <String, Object?>{};
  }

  /// A word of the page for what is wrong with an answer, or null when it is good.
  String? problem(HumanRequest r) {
    final v = answers[r.nodeId];
    if (r.task) {
      final state = _task(r)['state'];
      return state == 'done' || state == 'later' ? null : 'hr.need.task';
    }
    if (r.approve) return v is bool ? null : 'hr.need.yesno';
    final t = '${v ?? ''}'.trim();
    if (r.kind == 'choice') {
      if (r.choices.any((c) => c.value == t)) return null;
      if (r.other && t.isNotEmpty) return null;
      return r.choices.length == 2 ? 'hr.need.pick2' : r.other ? (other.contains(r.nodeId) ? 'hr.need' : 'hr.need.pick') : 'hr.need.choice';
    }
    if (t.isEmpty) return 'hr.need';
    if (r.kind == 'number' && num.tryParse(t) == null) return 'hr.need.number';
    return null;
  }

  /// Goes on if the answer is good (after a change made from the summary, straight back to it); else says what is wrong.
  String? next() {
    if (inSummary) return null;
    final bad = problem(items[step]);
    if (bad != null) return bad;
    final r = items[step];
    if (!r.approve && !r.task) answers[r.nodeId] = '${answers[r.nodeId]}'.trim();
    step = fromSummary ? items.length : step + 1;
    fromSummary = false;
    return null;
  }

  void prev() {
    if (step > 0) step -= 1;
    fromSummary = false;
  }

  void edit(int i) {
    step = i;
    fromSummary = true;
  }

  int get firstMissing => items.indexWhere((r) => problem(r) != null);

  /// Sets the answer of a task: whether it is done or cannot be now, and its note.
  void setTask(HumanRequest r, {String? state, String? note}) {
    final v = _task(r);
    answers[r.nodeId] = {'state': state ?? v['state'], 'note': note ?? v['note'] ?? ''};
  }

  /// The answers as the PC takes them: step id -> `true`/`false`, the text, or for a task `{done: true, note}`. A task that cannot be done now is left out.
  Map<String, Object> get payload {
    final out = <String, Object>{};
    for (final r in items) {
      if (r.task) {
        final v = _task(r);
        if (v['state'] == 'done') out[r.nodeId] = {'done': true, 'note': '${v['note'] ?? ''}'.trim()};
      } else {
        out[r.nodeId] = answers[r.nodeId] as Object;
      }
    }
    return out;
  }

  /// The tasks that cannot be done now: they come back in ten minutes.
  List<String> get notNow => [for (final r in items) if (r.task && _task(r)['state'] == 'later') r.nodeId];

  String shown(HumanRequest r) {
    final v = answers[r.nodeId];
    if (r.task) {
      final t = _task(r);
      final note = '${t['note'] ?? ''}'.trim();
      return t['state'] == 'later' ? Texts.t('hr.task.later') : t['state'] == 'done' ? Texts.t('hr.task.done') + (note.isEmpty ? '' : ': $note') : '';
    }
    if (r.approve) return v == true ? Texts.t('hr.answer.yes') : v == false ? Texts.t('hr.answer.no') : '';
    if (r.kind == 'choice') {
      for (final c in r.choices) {
        if (c.value == v) return c.shown;
      }
    }
    return '${v ?? ''}';
  }

  /// The path when the questions that are asking changed under it: the answers given stay, the step stays where it can.
  HumanPath reconcile(List<HumanRequest> now) {
    final fresh = HumanPath(now, answers);
    fresh.step = step < now.length ? step : now.length;
    return fresh;
  }
}

enum HumanOutcome { answered, later }

/// Looks at the PC's list every few seconds and hands the first plan that was not left for later to `show`, one window at a time. Later leaves a plan's questions for
/// `snooze` with the answers kept; so does "I can't now" for a task. An answered plan is gone from the list, and the next is looked for at once.
class HumanPoller {
  HumanPoller({
    required this.fetch,
    required this.show,
    this.every = const Duration(seconds: 4),
    this.snooze = const Duration(minutes: 10),
    DateTime Function()? clock,
  }) : clock = clock ?? DateTime.now;

  final Future<List<HumanRequest>> Function() fetch;
  final Future<HumanOutcome> Function(HumanGroup group, HumanPath? draft) show;
  final Duration every, snooze;
  final DateTime Function() clock;
  final Map<String, DateTime> _snoozed = {};
  final Map<String, HumanPath> drafts = {};
  Timer? _timer;
  bool _busy = false;

  bool get running => _timer != null;

  void start() {
    _timer ??= Timer.periodic(every, (_) => refresh());
    refresh();
  }

  void stop() {
    _timer?.cancel();
    _timer = null;
  }

  Future<void> refresh() async {
    if (_busy) return;
    _busy = true;
    var again = false;
    try {
      final all = await fetch();
      final now = clock();
      bool held(String key) => _snoozed[key]?.isAfter(now) ?? false;
      final group = HumanGroup.first(all, (r) => held(r.planId) || held('${r.planId}/${r.nodeId}'));
      if (group != null && running) {
        final outcome = await show(group, drafts[group.planId]);
        if (outcome == HumanOutcome.later) {
          _snoozed[group.planId] = clock().add(snooze);
        } else {
          for (final id in drafts[group.planId]?.notNow ?? const <String>[]) {
            _snoozed['${group.planId}/$id'] = clock().add(snooze);
          }
          drafts.remove(group.planId);
        }
        again = true;
      }
    } catch (_) {
      // the PC is not reachable now: the next look tries again
    } finally {
      _busy = false;
    }
    if (again && running) await refresh();
  }
}

/// The window: one question at a time ("Question 3 of 22"), Back and Next (and a swipe, and the arrow keys of a keyboard), then the summary of all the answers
/// with Edit for each and one Send for all of them. Two possible answers are two buttons; more are a menu with "Other, I write"; a step for the person is its
/// description, a note and "I did it" / "I can't now". Later leaves it with the answers kept in `path`.
class HumanWizardDialog extends StatefulWidget {
  const HumanWizardDialog({super.key, required this.group, required this.send, this.draft, this.onPath});
  final HumanGroup group;
  final HumanPath? draft;

  /// Sends all the answers together to the PC.
  final Future<void> Function(Map<String, Object> answers) send;

  /// Called with the path as it is, so that Later can keep it.
  final void Function(HumanPath path)? onPath;

  @override
  State<HumanWizardDialog> createState() => _HumanWizardDialogState();
}

const _otherValue = '__other__';

class _HumanWizardDialogState extends State<HumanWizardDialog> {
  late HumanPath path;
  final text = TextEditingController();
  String? error;
  bool sending = false;

  @override
  void initState() {
    super.initState();
    final kept = widget.draft;
    path = kept != null && kept.items.map((r) => r.nodeId).join('|') == widget.group.ids ? kept : HumanPath(widget.group.items, kept?.answers);
    widget.onPath?.call(path);
    _syncText();
  }

  @override
  void dispose() {
    text.dispose();
    super.dispose();
  }

  HumanRequest? get current => path.inSummary ? null : path.items[path.step];

  bool _typing(HumanRequest? r) => r != null && !r.twoWays && !r.task && (r.kind != 'choice' || path.other.contains(r.nodeId));

  void _syncText() {
    final r = current;
    if (r == null) {
      text.text = '';
    } else if (r.task) {
      text.text = '${(path.answers[r.nodeId] as Map?)?['note'] ?? ''}';
    } else {
      text.text = _typing(r) ? '${path.answers[r.nodeId] ?? ''}' : '';
    }
  }

  void _go(void Function() move) {
    setState(() {
      error = null;
      move();
      _syncText();
    });
    widget.onPath?.call(path);
  }

  void _next() {
    final r = current;
    if (r != null && r.task) {
      path.setTask(r, note: text.text);
    } else if (_typing(r)) {
      path.answers[r!.nodeId] = text.text;
    }
    final bad = path.next();
    setState(() {
      error = bad == null ? null : Texts.t(bad);
      if (bad == null) _syncText();
    });
    widget.onPath?.call(path);
  }

  Future<void> _send() async {
    final bad = path.firstMissing;
    if (bad >= 0) {
      _go(() => path.edit(bad));
      setState(() => error = Texts.t(path.problem(path.items[bad])!));
      return;
    }
    final body = path.payload;
    if (body.isEmpty) {                                          // every step was "I can't now": nothing to send, they come back later
      Navigator.of(context).pop(HumanOutcome.answered);
      return;
    }
    setState(() {
      sending = true;
      error = null;
    });
    try {
      await widget.send(body);
      if (mounted) Navigator.of(context).pop(HumanOutcome.answered);
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          sending = false;
          error = Texts.t('hr.err', [e.message]);
        });
      }
    }
  }

  void _later() => Navigator.of(context).pop(HumanOutcome.later);

  Widget _big(String label, VoidCallback? onPressed, {bool primary = false}) {
    final style = ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48)));
    return primary ? FilledButton(onPressed: onPressed, style: style, child: Text(label)) : OutlinedButton(onPressed: onPressed, style: style, child: Text(label));
  }

  /// Two buttons with the two answers in words; choosing one sends nothing.
  Widget _twoButtons(List<(String, Object)> pairs, Object? on, void Function(Object) set) {
    final style = ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48)));
    final buttons = <Widget>[];
    for (final (label, value) in pairs) {
      final chosen = on == value;
      void pick() => setState(() {
            set(value);
            error = null;
          });
      if (buttons.isNotEmpty) buttons.add(const SizedBox(width: 8));
      buttons.add(Expanded(child: Semantics(selected: chosen, child: chosen ? FilledButton(onPressed: pick, style: style, child: Text(label)) : OutlinedButton(onPressed: pick, style: style, child: Text(label)))));
    }
    return Row(children: buttons);
  }

  Widget _field(HumanRequest r) {
    if (r.task) {
      final state = (path.answers[r.nodeId] as Map?)?['state'];
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        TextField(
          key: ValueKey('note-${r.nodeId}'),
          controller: text,
          minLines: 2,
          maxLines: 4,
          decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.note')),
          onChanged: (x) => path.setTask(r, note: x),
        ),
        const SizedBox(height: 10),
        _twoButtons([(Texts.t('hr.done'), 'done'), (Texts.t('hr.cant'), 'later')], state, (v) => path.setTask(r, state: '$v', note: text.text)),
        if (state == 'later') Padding(padding: const EdgeInsets.only(top: 6), child: Text(Texts.t('hr.task.later.help'), style: Theme.of(context).textTheme.bodySmall)),
      ]);
    }
    if (r.approve) return _twoButtons([(Texts.t('hr.yes'), true), (Texts.t('hr.no'), false)], path.answers[r.nodeId], (v) => path.answers[r.nodeId] = v);
    if (r.kind == 'choice' && r.choices.length == 2) {
      return _twoButtons([for (final c in r.choices) (c.shown, c.value)], path.answers[r.nodeId], (v) => path.answers[r.nodeId] = v);
    }
    if (r.kind == 'choice') {
      final v = '${path.answers[r.nodeId] ?? ''}';
      final writing = path.other.contains(r.nodeId);
      final menu = DropdownButtonFormField<String>(
        key: ValueKey('choice-${r.nodeId}-$writing'),
        initialValue: writing ? _otherValue : (r.choices.any((c) => c.value == v) ? v : null),
        hint: Text(Texts.t('hr.choice')),
        isExpanded: true,
        items: [
          for (final c in r.choices) DropdownMenuItem(value: c.value, child: Text(c.shown)),
          if (r.other) DropdownMenuItem(value: _otherValue, child: Text(Texts.t('hr.other'))),
        ],
        onChanged: (x) => setState(() {
          error = null;
          if (x == _otherValue) {
            path.other.add(r.nodeId);
            path.answers[r.nodeId] = '';
            text.text = '';
          } else {
            path.other.remove(r.nodeId);
            path.answers[r.nodeId] = x;
          }
        }),
      );
      if (!writing) return menu;
      return Column(children: [
        menu,
        const SizedBox(height: 10),
        TextField(
          key: ValueKey('other-${r.nodeId}'),
          controller: text,
          autofocus: true,
          decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.other.write')),
          onChanged: (x) => path.answers[r.nodeId] = x,
          onSubmitted: (_) => _next(),
        ),
      ]);
    }
    return TextField(
      key: ValueKey('text-${r.nodeId}'),
      controller: text,
      autofocus: true,
      keyboardType: r.kind == 'number' ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
      decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.value')),
      onChanged: (x) => path.answers[r.nodeId] = x,
      onSubmitted: (_) => _next(),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    const gap = SizedBox(height: 10);
    final r = current;
    final n = path.items.length;
    final children = <Widget>[Text(widget.group.planTitle, style: theme.textTheme.bodySmall)];
    if (r != null) {
      children.addAll([
        Row(children: [
          Text(Texts.t('hr.q', [path.step + 1, n]), style: theme.textTheme.bodySmall),
          const SizedBox(width: 12),
          Expanded(child: LinearProgressIndicator(value: (path.step + 1) / n)),
        ]),
        gap,
        Text(r.question, style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        if (r.explanation.isNotEmpty) ...[const SizedBox(height: 6), Text(r.explanation, style: theme.textTheme.bodyMedium?.copyWith(color: theme.hintColor))],
        if (r.content.isNotEmpty) ...[
          gap,
          if (r.task) Text(Texts.t('hr.task.what'), style: theme.textTheme.bodySmall),
          Container(
            constraints: const BoxConstraints(maxHeight: 180),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(border: Border.all(color: theme.dividerColor), borderRadius: BorderRadius.circular(6)),
            child: SingleChildScrollView(child: SelectableText(r.content)),
          ),
        ],
        gap,
        _field(r),
        if (error != null) ...[gap, Text(error!, style: TextStyle(color: theme.colorScheme.error))],
        gap,
        Row(children: [
          Expanded(child: _big(Texts.t('hr.prev'), path.step == 0 ? null : () => _go(path.prev))),
          const SizedBox(width: 8),
          Expanded(child: _big(Texts.t('hr.next'), _next, primary: true)),
        ]),
      ]);
    } else {
      children.addAll([
        gap,
        Text(Texts.t('hr.review'), style: theme.textTheme.titleLarge?.copyWith(fontWeight: FontWeight.w600)),
        const SizedBox(height: 6),
        Text(Texts.t('hr.review.help'), style: theme.textTheme.bodyMedium?.copyWith(color: theme.hintColor)),
        gap,
        for (var i = 0; i < n; i++)
          Card(
            margin: const EdgeInsets.symmetric(vertical: 3),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(12, 6, 4, 6),
              child: Row(children: [
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(path.items[i].question, style: theme.textTheme.bodySmall),
                    Text(path.problem(path.items[i]) != null ? Texts.t(path.problem(path.items[i])!) : path.shown(path.items[i]), style: const TextStyle(fontWeight: FontWeight.w600)),
                  ]),
                ),
                TextButton(
                  style: ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48))),
                  onPressed: sending ? null : () => _go(() => path.edit(i)),
                  child: Text(Texts.t('hr.edit')),
                ),
              ]),
            ),
          ),
        if (error != null) ...[gap, Text(error!, style: TextStyle(color: theme.colorScheme.error))],
        gap,
        Row(children: [
          Expanded(child: _big(Texts.t('hr.prev'), sending ? null : () => _go(path.prev))),
          const SizedBox(width: 8),
          Expanded(flex: 2, child: _big(Texts.t('hr.send'), sending ? null : _send, primary: true)),
        ]),
      ]);
    }
    children.addAll([
      const SizedBox(height: 6),
      TextButton(style: ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48))), onPressed: sending ? null : _later, child: Text(Texts.t('hr.later'))),
      Text(Texts.t('hr.later.help'), style: theme.textTheme.bodySmall),
    ]);
    final typing = _typing(r) || (r?.task ?? false);
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && !sending) _later();
      },
      child: CallbackShortcuts(
        bindings: typing
            ? const {}
            : {
                const SingleActivator(LogicalKeyboardKey.arrowRight): () {
                  if (!path.inSummary && !sending) _next();
                },
                const SingleActivator(LogicalKeyboardKey.arrowLeft): () {
                  if (path.step > 0 && !sending) _go(path.prev);
                },
              },
        child: Focus(
          autofocus: !typing,
          child: GestureDetector(
            behavior: HitTestBehavior.translucent,
            onHorizontalDragEnd: (d) {
              final v = d.primaryVelocity ?? 0;
              if (sending) return;
              if (v < -300 && !path.inSummary) _next();
              if (v > 300 && path.step > 0) _go(path.prev);
            },
            child: Dialog(
              insetPadding: const EdgeInsets.all(16),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 560),
                child: SingleChildScrollView(padding: const EdgeInsets.all(16), child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: children)),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
