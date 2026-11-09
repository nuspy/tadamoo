import 'dart:math';

import 'package:flutter/material.dart';

import 'avatar_controller.dart';
import 'gateway_client.dart';
import 'i18n.dart';

/// The Avatar tab. It connects while it is shown and lets go of the microphone, the speaker and the video when it is not.
class AvatarScreen extends StatefulWidget {
  const AvatarScreen({super.key, required this.controller, required this.active});
  final AvatarController controller;
  final bool active;

  @override
  State<AvatarScreen> createState() => _AvatarScreenState();
}

class _AvatarScreenState extends State<AvatarScreen> with WidgetsBindingObserver {
  final typed = TextEditingController();

  AvatarController get c => widget.controller;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    if (widget.active) c.activate();
  }

  @override
  void didUpdateWidget(AvatarScreen old) {
    super.didUpdateWidget(old);
    if (widget.active != old.active) {
      widget.active ? c.activate() : c.deactivate();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused) {
      c.deactivate();
    } else if (state == AppLifecycleState.resumed && widget.active) {
      c.activate();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    typed.dispose();
    super.dispose();
  }

  String _status() {
    final g = c.client;
    if (c.unavailable != null) return '${c.unavailable} ${Texts.t('avatar.retrying')}';
    switch (g.status) {
      case GatewayStatus.offline:
        return g.problemCode == 'unauthorized' ? Texts.t('avatar.err.unauthorized') : Texts.t('avatar.offline');
      case GatewayStatus.connecting:
        return Texts.t('avatar.connecting');
      case GatewayStatus.ready:
        if (!g.voice) return Texts.t('avatar.novoice', [g.missing.map((m) => Texts.t('avatar.$m')).join(', ')]);
        return Texts.t('avatar.connected');
    }
  }

  String _note() {
    final g = c.client;
    if (c.mode != 'video') return Texts.t('avatar.mode.later');
    if (g.status != GatewayStatus.ready) return '';
    if (!c.devices.videoSupported) return Texts.t('avatar.video.unsupported');
    if (!g.avatar) return g.videoReason == 'no_webrtc' ? Texts.t('avatar.video.nowebrtc') : Texts.t('avatar.novideo');
    if (c.liveVideo == null) return Texts.t('avatar.video.wait', [g.videoDetail]);
    return '';
  }

  Widget _stage(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final video = c.liveVideo;
    final g = c.client;
    final icon = switch (g.phase) {
      Phase.listening => Icons.mic,
      Phase.thinking => Icons.more_horiz,
      Phase.speaking => Icons.graphic_eq,
      Phase.idle => Icons.record_voice_over_outlined,
    };
    return Container(
      decoration: BoxDecoration(color: const Color(0xFF171C21), borderRadius: BorderRadius.circular(12)),
      clipBehavior: Clip.antiAlias,
      child: video != null
          ? video.view()
          : Center(
              child: Semantics(
                label: _phaseText(),
                child: Icon(icon, size: 64, color: g.phase == Phase.idle ? scheme.outline : scheme.primary),
              ),
            ),
    );
  }

  String _phaseText() {
    switch (c.client.phase) {
      case Phase.listening:
        return Texts.t('avatar.release');
      case Phase.thinking:
        return Texts.t('avatar.thinking');
      case Phase.speaking:
        return Texts.t('avatar.speaking');
      case Phase.idle:
        return Texts.t('avatar.hold');
    }
  }

  Widget _modes() {
    return Wrap(
      spacing: 8,
      children: [
        for (final m in avatarModes)
          ChoiceChip(
            label: Text(Texts.t('avatar.mode.$m')),
            selected: c.mode == m,
            onSelected: (_) => c.setMode(m),
          ),
      ],
    );
  }

  Widget _brain() {
    final g = c.client;
    return Row(
      children: [
        Expanded(
          child: SegmentedButton<String>(
            segments: [
              ButtonSegment(value: 'direct', label: Text(Texts.t('avatar.brain.direct'))),
              ButtonSegment(value: 'harness', label: Text(Texts.t('avatar.brain.harness'))),
            ],
            selected: {g.brain == 'harness' ? 'harness' : 'direct'},
            onSelectionChanged: (s) => c.setBrain(s.first),
          ),
        ),
        IconButton(
          tooltip: Texts.t('avatar.brain.help'),
          icon: const Icon(Icons.info_outline),
          onPressed: () => showDialog<void>(
            context: context,
            builder: (d) => AlertDialog(
              title: Text(Texts.t('avatar.brain')),
              content: Text(Texts.t('avatar.brain.help')),
              actions: [TextButton(onPressed: () => Navigator.pop(d), child: const Text('OK'))],
            ),
          ),
        ),
      ],
    );
  }

  Widget _persona() {
    final g = c.client;
    final slugs = c.personas.map((p) => p['slug']).toSet();
    final current = slugs.contains(g.persona) ? g.persona : '';
    return DropdownButtonFormField<String>(
      key: ValueKey('persona-$current-${c.personas.length}'),
      initialValue: current,
      isExpanded: true,
      decoration: InputDecoration(labelText: Texts.t('avatar.persona'), border: const OutlineInputBorder()),
      items: [
        DropdownMenuItem(value: '', child: Text(Texts.t('avatar.persona.default'))),
        for (final p in c.personas) DropdownMenuItem(value: p['slug']!, child: Text(p['name']!, overflow: TextOverflow.ellipsis)),
      ],
      onChanged: (v) => c.setPersona(v ?? ''),
    );
  }

  Widget _lines() {
    final lines = c.client.lines;
    if (lines.isEmpty) {
      return Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('avatar.empty'), textAlign: TextAlign.center));
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final line in lines)
          Align(
            alignment: line.mine ? Alignment.centerRight : Alignment.centerLeft,
            child: Container(
              margin: const EdgeInsets.symmetric(vertical: 4),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.85),
              decoration: BoxDecoration(
                color: line.mine ? Theme.of(context).colorScheme.primaryContainer : const Color(0xFF171C21),
                borderRadius: BorderRadius.circular(12),
              ),
              child: SelectableText(line.text, style: const TextStyle(height: 1.5)),
            ),
          ),
      ],
    );
  }

  Widget _talkButton() {
    final g = c.client;
    final enabled = g.canTalk;
    final scheme = Theme.of(context).colorScheme;
    final speaking = g.phase == Phase.speaking;
    final listening = g.phase == Phase.listening;
    final label = _phaseText();
    return Semantics(
      button: true,
      enabled: enabled,
      label: label,
      onTap: enabled ? c.toggleTalk : null,
      excludeSemantics: true,
      child: Listener(
        onPointerDown: (_) {
          if (!enabled) return;
          if (speaking) {
            c.stopSpeaking();
          }
          c.holdStart();
        },
        onPointerUp: (_) => c.holdEnd(),
        onPointerCancel: (_) => c.holdEnd(),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 120),
          height: 72,
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: !enabled ? scheme.surfaceContainerHighest : (listening ? scheme.primary : scheme.primaryContainer),
            borderRadius: BorderRadius.circular(36),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(listening ? Icons.mic : Icons.mic_none, color: listening ? scheme.onPrimary : scheme.onPrimaryContainer),
              const SizedBox(width: 12),
              Flexible(child: Text(label, overflow: TextOverflow.ellipsis, style: TextStyle(fontWeight: FontWeight.w600, color: listening ? scheme.onPrimary : scheme.onPrimaryContainer))),
            ],
          ),
        ),
      ),
    );
  }

  Widget _typing() {
    return Row(
      children: [
        Expanded(
          child: TextField(
            controller: typed,
            decoration: InputDecoration(hintText: Texts.t('avatar.type'), border: const OutlineInputBorder(), isDense: false),
            textInputAction: TextInputAction.send,
            onSubmitted: (_) => _sendTyped(),
          ),
        ),
        IconButton(tooltip: Texts.t('avatar.send'), onPressed: _sendTyped, icon: const Icon(Icons.send)),
      ],
    );
  }

  void _sendTyped() {
    if (c.sendText(typed.text)) typed.clear();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: c,
      builder: (context, _) {
        final width = min(MediaQuery.sizeOf(context).width, 640.0);
        final note = _note();
        final problem = c.micDenied ? Texts.t('avatar.mic.denied') : (c.client.problemCode == 'not_loaded' ? Texts.t('avatar.err.not_loaded') : c.client.problem);
        final style = Theme.of(context).textTheme;
        return Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 640),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(
                  child: ListView(
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
                    children: [
                      Text(_status(), style: style.bodyMedium),
                      const SizedBox(height: 8),
                      _modes(),
                      const SizedBox(height: 8),
                      SizedBox(height: (width - 32) * 0.56, child: _stage(context)),
                      if (note.isNotEmpty) Padding(padding: const EdgeInsets.only(top: 8), child: Text(note, style: style.bodySmall)),
                      if (problem != null && problem.isNotEmpty)
                        Padding(padding: const EdgeInsets.only(top: 8), child: Text(problem, style: TextStyle(color: Theme.of(context).colorScheme.error))),
                      const SizedBox(height: 8),
                      _lines(),
                      const SizedBox(height: 8),
                      _persona(),
                      const SizedBox(height: 8),
                      _typing(),
                    ],
                  ),
                ),
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
                  child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [_brain(), const SizedBox(height: 4), _talkButton()]),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}
