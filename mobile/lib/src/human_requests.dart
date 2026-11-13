import 'dart:async';

import 'package:flutter/material.dart';

import 'api.dart';
import 'i18n.dart';

/// What a plan asks the person: to approve what it shows (yes or no), or to give a value. The PC lists these for all the plans (`/api/plans/human`).
class HumanRequest {
  const HumanRequest({
    required this.planId,
    required this.planTitle,
    required this.nodeId,
    required this.title,
    required this.ask,
    required this.prompt,
    this.content = '',
    this.kind = 'text',
    this.choices = const [],
  });

  factory HumanRequest.fromJson(Map<String, dynamic> j) => HumanRequest(
        planId: '${j['plan_id'] ?? ''}',
        planTitle: '${j['plan_title'] ?? ''}',
        nodeId: '${j['node_id'] ?? ''}',
        title: '${j['title'] ?? ''}',
        ask: '${j['ask'] ?? 'value'}',
        prompt: '${j['prompt'] ?? ''}',
        content: '${j['content'] ?? ''}',
        kind: '${j['kind'] ?? 'text'}',
        choices: [for (final c in (j['choices'] as List?) ?? const []) '$c'],
      );

  final String planId, planTitle, nodeId, title, ask, prompt, content, kind;
  final List<String> choices;
  String get key => '$planId/$nodeId';
  bool get approve => ask == 'approve';
}

enum HumanOutcome { answered, later }

/// Looks at the PC's list every few seconds and hands the oldest request that was not left for later to `show`, one window at a time.
/// Back leaves a request for `snooze`; an answered one is gone from the list, and the next is looked for at once.
class HumanPoller {
  HumanPoller({
    required this.fetch,
    required this.show,
    this.every = const Duration(seconds: 4),
    this.snooze = const Duration(minutes: 10),
    DateTime Function()? clock,
  }) : clock = clock ?? DateTime.now;

  final Future<List<HumanRequest>> Function() fetch;
  final Future<HumanOutcome> Function(HumanRequest request, int index, int total) show;
  final Duration every, snooze;
  final DateTime Function() clock;
  final Map<String, DateTime> _snoozed = {};
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
      final open = [for (final r in all) if (!(_snoozed[r.key]?.isAfter(now) ?? false)) r];
      if (open.isNotEmpty && running) {
        final outcome = await show(open.first, 1, open.length);
        if (outcome == HumanOutcome.later) {
          _snoozed[open.first.key] = clock().add(snooze);
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

/// The window: choose (Yes or No; or a value and OK), then confirm. Back leaves it for later; from the confirmation it returns to the choice.
class HumanRequestDialog extends StatefulWidget {
  const HumanRequestDialog({super.key, required this.request, required this.send, this.index = 1, this.total = 1});
  final HumanRequest request;
  final int index, total;

  /// Sends the answer to the PC: `true` or `false` for an approval, the text (or number as text) for a value.
  final Future<void> Function(Object answer) send;

  @override
  State<HumanRequestDialog> createState() => _HumanRequestDialogState();
}

class _HumanRequestDialogState extends State<HumanRequestDialog> {
  final text = TextEditingController();
  bool confirming = false;
  bool sending = false;
  Object? chosen;
  String? error;
  String? picked;

  HumanRequest get r => widget.request;

  @override
  void dispose() {
    text.dispose();
    super.dispose();
  }

  void _back() {
    if (confirming) {
      setState(() {
        confirming = false;
        error = null;
      });
    } else {
      Navigator.of(context).pop(HumanOutcome.later);
    }
  }

  void _choose(bool yes) => setState(() {
        chosen = yes;
        confirming = true;
        error = null;
      });

  void _proceed() {
    final value = r.kind == 'choice' ? (picked ?? '') : text.text.trim();
    if (value.isEmpty) {
      setState(() => error = Texts.t('hr.need'));
      return;
    }
    if (r.kind == 'number' && num.tryParse(value) == null) {
      setState(() => error = Texts.t('hr.need.number'));
      return;
    }
    setState(() {
      chosen = value;
      confirming = true;
      error = null;
    });
  }

  Future<void> _send() async {
    setState(() {
      sending = true;
      error = null;
    });
    try {
      await widget.send(chosen!);
      if (mounted) Navigator.of(context).pop(HumanOutcome.answered);
    } on ApiError catch (e) {
      if (mounted) {
        setState(() {
          sending = false;
          confirming = false;
          error = Texts.t('hr.err', [e.message]);
        });
      }
    }
  }

  Widget _big(String label, VoidCallback? onPressed, {bool primary = false}) {
    final style = ButtonStyle(minimumSize: WidgetStateProperty.all(const Size.fromHeight(48)));
    return primary
        ? FilledButton(onPressed: onPressed, style: style, child: Text(label))
        : OutlinedButton(onPressed: onPressed, style: style, child: Text(label));
  }

  String get _shown => r.approve ? Texts.t(chosen == true ? 'hr.answer.yes' : 'hr.answer.no') : '$chosen';

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final gap = const SizedBox(height: 10);
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _back();
      },
      child: Dialog(
        insetPadding: const EdgeInsets.all(16),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 560),
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(16),
            child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
              if (widget.total > 1) Text(Texts.t('hr.count', [widget.index, widget.total]), style: theme.textTheme.bodySmall),
              Text(Texts.t('hr.title'), style: theme.textTheme.titleMedium, semanticsLabel: Texts.t('hr.title')),
              gap,
              Text(r.planTitle, style: theme.textTheme.titleSmall),
              Text(Texts.t('hr.step', [r.title]), style: theme.textTheme.bodySmall),
              gap,
              Text(r.prompt),
              if (r.content.isNotEmpty) ...[
                gap,
                Text(Texts.t('hr.content'), style: theme.textTheme.bodySmall),
                Container(
                  constraints: const BoxConstraints(maxHeight: 220),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(border: Border.all(color: theme.dividerColor), borderRadius: BorderRadius.circular(6)),
                  child: SingleChildScrollView(child: SelectableText(r.content)),
                ),
              ],
              gap,
              if (confirming) ...[
                Text(Texts.t('hr.confirm.you', [_shown]), style: theme.textTheme.titleSmall),
                Text(Texts.t('hr.confirm.q')),
                gap,
                _big(Texts.t('hr.confirm.send'), sending ? null : _send, primary: true),
                const SizedBox(height: 8),
                _big(Texts.t('hr.back'), sending ? null : _back),
              ] else if (r.approve) ...[
                _big(Texts.t('hr.yes'), () => _choose(true), primary: true),
                const SizedBox(height: 8),
                _big(Texts.t('hr.no'), () => _choose(false)),
                const SizedBox(height: 8),
                _big(Texts.t('hr.back'), _back),
              ] else ...[
                if (r.kind == 'choice')
                  DropdownButtonFormField<String>(
                    initialValue: picked,
                    hint: Text(Texts.t('hr.choice')),
                    isExpanded: true,
                    items: [for (final c in r.choices) DropdownMenuItem(value: c, child: Text(c))],
                    onChanged: (v) => setState(() => picked = v),
                  )
                else
                  TextField(
                    controller: text,
                    autofocus: true,
                    keyboardType: r.kind == 'number' ? const TextInputType.numberWithOptions(decimal: true) : TextInputType.text,
                    decoration: InputDecoration(labelText: Texts.t('hr.value'), border: const OutlineInputBorder()),
                    onSubmitted: (_) => _proceed(),
                  ),
                gap,
                _big(Texts.t('hr.ok'), _proceed, primary: true),
                const SizedBox(height: 8),
                _big(Texts.t('hr.back'), _back),
              ],
              if (error != null) ...[gap, Text(error!, style: TextStyle(color: theme.colorScheme.error))],
              if (!confirming) ...[gap, Text(Texts.t('hr.back.help'), style: theme.textTheme.bodySmall)],
            ]),
          ),
        ),
      ),
    );
  }
}
