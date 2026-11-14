import 'dart:async';

import 'package:flutter/material.dart';

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
    this.group = '',
    this.groupEn = '',
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
        group: '${j['group'] ?? ''}',
        groupEn: '${j['group_en'] ?? ''}',
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

  final String planId, planTitle, nodeId, title, ask, prompt, promptEn, help, helpEn, group, groupEn, content, kind;
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
  String get heading => Texts.language == 'en' ? (groupEn.isEmpty ? group : groupEn) : (group.isEmpty ? groupEn : group);
}

/// The requests of one plan that are asking now, ALL of them, in the order of the plan: one list.
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

/// The answers of a list of questions. They start with what the plan proposes (or what was answered the last time). The answer to a task is
/// `{'state': 'done' | 'later', 'note': text}`.
class HumanForm {
  HumanForm(this.items, [Map<String, Object?>? before])
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

  /// The questions that have no good answer yet.
  List<HumanRequest> get unanswered => [for (final r in items) if (problem(r) != null) r];

  /// Sets the answer of a task: whether it is done or cannot be now, and its note.
  void setTask(HumanRequest r, {String? state, String? note}) {
    final v = _task(r);
    answers[r.nodeId] = {'state': state ?? v['state'], 'note': note ?? v['note'] ?? ''};
  }

  /// The answers as the PC takes them, all together: step id -> `true`/`false`, the text, or for a task `{done: true, note}`. A task that cannot be done now is left out.
  Map<String, Object> get payload {
    final out = <String, Object>{};
    for (final r in items) {
      if (r.task) {
        final v = _task(r);
        if (v['state'] == 'done') out[r.nodeId] = {'done': true, 'note': '${v['note'] ?? ''}'.trim()};
      } else {
        final v = answers[r.nodeId] as Object;
        out[r.nodeId] = v is String ? v.trim() : v;
      }
    }
    return out;
  }

  /// The tasks that cannot be done now: they come back in ten minutes.
  List<String> get notNow => [for (final r in items) if (r.task && _task(r)['state'] == 'later') r.nodeId];

  /// The list again, when the questions that are asking changed: the answers given stay.
  HumanForm reconcile(List<HumanRequest> now) => HumanForm(now, answers);
}

/// The items of the list with the heading to put before each, when the plan groups them and the group changes.
List<(HumanRequest, String)> humanSections(List<HumanRequest> items) {
  String? before;
  final out = <(HumanRequest, String)>[];
  for (final r in items) {
    final head = r.heading;
    out.add((r, head.isNotEmpty && head != before ? head : ''));
    if (head.isNotEmpty) before = head;
  }
  return out;
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
  final Future<HumanOutcome> Function(HumanGroup group, HumanForm? draft) show;
  final Duration every, snooze;
  final DateTime Function() clock;
  final Map<String, DateTime> _snoozed = {};
  final Map<String, HumanForm> drafts = {};
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

/// The window: all the questions of a plan in one scrolling list (full screen on a phone, a wide panel on a larger screen), under headings when the plan groups them, each with its
/// explanation and its field; at the bottom, always in sight, one Send for all the answers with how many are not answered yet. Two possible answers are two buttons; more are a
/// menu with "Other, I write"; a step for the person is its description, a note and "I did it" / "I can't now". Later leaves it with the answers kept in `form`.
class HumanListDialog extends StatefulWidget {
  const HumanListDialog({super.key, required this.group, required this.send, this.draft, this.onForm});
  final HumanGroup group;
  final HumanForm? draft;

  /// Sends all the answers together to the PC.
  final Future<void> Function(Map<String, Object> answers) send;

  /// Called with the form as it is, so that Later can keep it.
  final void Function(HumanForm form)? onForm;

  @override
  State<HumanListDialog> createState() => _HumanListDialogState();
}

const _otherValue = '__other__';

class _HumanListDialogState extends State<HumanListDialog> {
  late HumanForm form;
  final controllers = <String, TextEditingController>{};
  final keys = <String, GlobalKey>{};
  final errors = <String, String>{};
  String? error;
  bool sending = false;

  @override
  void initState() {
    super.initState();
    final kept = widget.draft;
    form = kept != null && kept.items.map((r) => r.nodeId).join('|') == widget.group.ids ? kept : HumanForm(widget.group.items, kept?.answers);
    widget.onForm?.call(form);
  }

  @override
  void dispose() {
    for (final c in controllers.values) {
      c.dispose();
    }
    super.dispose();
  }

  TextEditingController _controller(String id, String initial) => controllers.putIfAbsent(id, () => TextEditingController(text: initial));

  void _changed(HumanRequest r, VoidCallback change) {
    setState(() {
      change();
      errors.remove(r.nodeId);
      error = null;
    });
    widget.onForm?.call(form);
  }

  Future<void> _send() async {
    final open = form.unanswered;
    if (open.isNotEmpty) {
      setState(() {
        for (final r in open) {
          errors[r.nodeId] = Texts.t(form.problem(r)!);
        }
        error = Texts.t('hr.left', [open.length]);
      });
      final context = keys[open.first.nodeId]?.currentContext;
      if (context != null) await Scrollable.ensureVisible(context, alignment: 0.2, duration: const Duration(milliseconds: 200));
      return;
    }
    final body = form.payload;
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

  ButtonStyle get _big => ButtonStyle(minimumSize: WidgetStateProperty.all(const Size(48, 48)));

  /// Two buttons with the two answers in words; choosing one sends nothing.
  Widget _twoButtons(HumanRequest r, List<(String, Object)> pairs, Object? on, void Function(Object) set) {
    final buttons = <Widget>[];
    for (final (label, value) in pairs) {
      final chosen = on == value;
      void pick() => _changed(r, () => set(value));
      if (buttons.isNotEmpty) buttons.add(const SizedBox(width: 8));
      buttons.add(Expanded(child: Semantics(selected: chosen, child: chosen ? FilledButton(onPressed: pick, style: _big, child: Text(label)) : OutlinedButton(onPressed: pick, style: _big, child: Text(label)))));
    }
    return Row(children: buttons);
  }

  Widget _field(HumanRequest r) {
    final id = r.nodeId;
    if (r.task) {
      final v = form.answers[id] as Map?;
      final state = v?['state'];
      return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        TextField(
          controller: _controller('note-$id', '${v?['note'] ?? ''}'),
          minLines: 2,
          maxLines: 4,
          decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.note')),
          onChanged: (x) {
            form.setTask(r, note: x);
            widget.onForm?.call(form);
          },
        ),
        const SizedBox(height: 10),
        _twoButtons(r, [(Texts.t('hr.done'), 'done'), (Texts.t('hr.cant'), 'later')], state, (x) => form.setTask(r, state: '$x', note: controllers['note-$id']?.text)),
        if (state == 'later') Padding(padding: const EdgeInsets.only(top: 6), child: Text(Texts.t('hr.task.later.help'), style: Theme.of(context).textTheme.bodySmall)),
      ]);
    }
    if (r.approve) return _twoButtons(r, [(Texts.t('hr.yes'), true), (Texts.t('hr.no'), false)], form.answers[id], (x) => form.answers[id] = x);
    if (r.kind == 'choice' && r.choices.length == 2) {
      return _twoButtons(r, [for (final c in r.choices) (c.shown, c.value)], form.answers[id], (x) => form.answers[id] = x);
    }
    if (r.kind == 'choice') {
      final v = '${form.answers[id] ?? ''}';
      final writing = form.other.contains(id);
      final menu = DropdownButtonFormField<String>(
        key: ValueKey('choice-$id-$writing'),
        initialValue: writing ? _otherValue : (r.choices.any((c) => c.value == v) ? v : null),
        hint: Text(Texts.t('hr.choice')),
        isExpanded: true,
        items: [
          for (final c in r.choices) DropdownMenuItem(value: c.value, child: Text(c.shown)),
          if (r.other) DropdownMenuItem(value: _otherValue, child: Text(Texts.t('hr.other'))),
        ],
        onChanged: (x) => _changed(r, () {
          if (x == _otherValue) {
            form.other.add(id);
            form.answers[id] = '';
            controllers['other-$id']?.text = '';
          } else {
            form.other.remove(id);
            form.answers[id] = x;
          }
        }),
      );
      if (!writing) return menu;
      return Column(children: [
        menu,
        const SizedBox(height: 10),
        TextField(
          controller: _controller('other-$id', v),
          autofocus: true,
          decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.other.write')),
          onChanged: (x) => _changed(r, () => form.answers[id] = x),
        ),
      ]);
    }
    return TextField(
      controller: _controller('text-$id', '${form.answers[id] ?? ''}'),
      keyboardType: r.kind == 'number' ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
      textInputAction: TextInputAction.next,
      decoration: InputDecoration(border: const OutlineInputBorder(), labelText: Texts.t('hr.value')),
      onChanged: (x) => _changed(r, () => form.answers[id] = x),
    );
  }

  Widget _item(HumanRequest r, int number) {
    final theme = Theme.of(context);
    final key = keys.putIfAbsent(r.nodeId, GlobalKey.new);
    final bad = errors[r.nodeId];
    return Padding(
      key: key,
      padding: const EdgeInsets.symmetric(vertical: 12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          SizedBox(width: 30, child: Text('$number', style: theme.textTheme.bodySmall)),
          Expanded(child: Text(r.question, style: theme.textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600))),
        ]),
        if (r.explanation.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 4), child: Text(r.explanation, style: theme.textTheme.bodyMedium?.copyWith(color: theme.hintColor))),
        if (r.content.isNotEmpty) ...[
          const SizedBox(height: 8),
          if (r.task) Text(Texts.t('hr.task.what'), style: theme.textTheme.bodySmall),
          Container(
            constraints: const BoxConstraints(maxHeight: 160),
            padding: const EdgeInsets.all(10),
            decoration: BoxDecoration(border: Border.all(color: theme.dividerColor), borderRadius: BorderRadius.circular(6)),
            child: SingleChildScrollView(child: SelectableText(r.content)),
          ),
        ],
        const SizedBox(height: 8),
        _field(r),
        if (bad != null) Padding(padding: const EdgeInsets.only(top: 6), child: Text(bad, style: TextStyle(color: theme.colorScheme.error))),
        const Divider(height: 24),
      ]),
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final n = form.items.length;
    final left = form.unanswered.length;
    final wide = MediaQuery.of(context).size.width >= 600;
    final children = <Widget>[];
    var number = 0;
    for (final (r, heading) in humanSections(form.items)) {
      number += 1;
      if (heading.isNotEmpty) {
        children.add(Padding(padding: const EdgeInsets.only(top: 14), child: Text(heading, style: theme.textTheme.titleSmall?.copyWith(color: theme.colorScheme.primary, fontWeight: FontWeight.w700))));
        children.add(const Divider(height: 10));
      }
      children.add(_item(r, number));
    }
    final body = SafeArea(
      child: Column(children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
          child: Align(
            alignment: Alignment.centerLeft,
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text(Texts.t('hr.title'), style: theme.textTheme.titleLarge),
              Text(widget.group.planTitle, style: theme.textTheme.titleSmall),
              Text(n == 1 ? Texts.t('hr.count.one') : Texts.t('hr.count', [n]), style: theme.textTheme.bodySmall),
            ]),
          ),
        ),
        const Divider(height: 1),
        Expanded(child: ListView(padding: const EdgeInsets.symmetric(horizontal: 16), children: children)),
        const Divider(height: 1),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(left > 0 ? Texts.t('hr.left', [left]) : Texts.t('hr.left.none'), style: TextStyle(fontWeight: left > 0 ? FontWeight.w600 : FontWeight.w400)),
            if (error != null) Text(error!, style: TextStyle(color: theme.colorScheme.error)),
            const SizedBox(height: 8),
            Row(children: [
              Expanded(child: OutlinedButton(style: _big, onPressed: sending ? null : _later, child: Text(Texts.t('hr.later')))),
              const SizedBox(width: 8),
              Expanded(flex: 2, child: FilledButton(style: _big, onPressed: sending ? null : _send, child: Text(Texts.t('hr.send')))),
            ]),
          ]),
        ),
      ]),
    );
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && !sending) _later();
      },
      child: wide
          ? Dialog(insetPadding: const EdgeInsets.all(16), child: ConstrainedBox(constraints: const BoxConstraints(maxWidth: 900), child: body))
          : Dialog.fullscreen(child: body),
    );
  }
}
