import 'dart:math';

import 'package:flutter/material.dart';

import 'audio_route.dart';
import 'avatar_controller.dart';
import 'gateway_client.dart';
import 'i18n.dart';
import 'lips_icon.dart';

/// The Avatar tab: the avatar, an accordion with the text chat (closed at first), and under them the conversation controls: who answers
/// (lips for Direct, brain for Harness), where the sound goes, and the hold-to-talk bar. How the avatar is drawn, its size and quality,
/// and the personality are in Settings.
class AvatarScreen extends StatefulWidget {
  const AvatarScreen({super.key, required this.controller, required this.active});
  final AvatarController controller;
  final bool active;

  @override
  State<AvatarScreen> createState() => _AvatarScreenState();
}

class _AvatarScreenState extends State<AvatarScreen> with WidgetsBindingObserver {
  final typed = TextEditingController();
  bool chatOpen = false;

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

  /// What the PC said the avatar is drawn by (`engine`, `resolution` of `ready` and `render_mode`), in words.
  String _engineLine() {
    final g = c.client;
    if (g.engine.isEmpty) return '';
    final name = {'visemes': 'Visemes', 'wav2lip_gan96': 'Wav2Lip GAN 96'}[g.engine] ?? g.engine;
    final h = int.tryParse(g.resolution) ?? 0;
    return Texts.t(h > 0 ? 'avatar.engine.line' : 'avatar.engine.line.plain', [name, h]);
  }

  String _note() {
    final g = c.client;
    if (!c.avatarOn) return Texts.t('avatar.off.note');
    if (c.rendererNote.isNotEmpty) return c.rendererNote;
    if (g.status != GatewayStatus.ready) return '';
    if (c.renderer != null) return _engineLine();
    final rm = g.renderMode;
    if (rm == 'visemes' || rm == 'wav2lip') return Texts.t('avatar.render.preparing');
    if (c.mode == 'video' || rm == 'video') {
      if (!c.devices.videoSupported) return Texts.t('avatar.video.unsupported');
      if (g.videoReason == 'emergency_only') return Texts.t('avatar.video.emergency');
      if (g.videoReason == 'no_webrtc') return Texts.t('avatar.video.nowebrtc');
      if (g.videoReason == 'no_avatar' || !g.avatar) return Texts.t('avatar.novideo');
      if (c.liveVideo == null) return Texts.t('avatar.video.wait', [g.videoDetail]);
      return '';
    }
    return Texts.t('avatar.render.none');
  }

  IconData _phaseIcon() {
    switch (c.client.phase) {
      case Phase.listening:
        return Icons.mic;
      case Phase.thinking:
        return Icons.more_horiz;
      case Phase.speaking:
        return Icons.graphic_eq;
      case Phase.idle:
        return Icons.record_voice_over_outlined;
    }
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

  Widget _stage(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final video = c.liveVideo;
    final drawn = c.renderer;
    final idle = c.client.phase == Phase.idle;
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 16),
      decoration: BoxDecoration(color: const Color(0xFF171C21), borderRadius: BorderRadius.circular(12)),
      clipBehavior: Clip.antiAlias,
      child: drawn != null
          ? Semantics(label: _phaseText(), child: drawn.view())
          : video != null
              ? video.view()
              : Center(
                  child: Semantics(label: _phaseText(), child: Icon(_phaseIcon(), size: 64, color: idle ? scheme.outline : scheme.primary)),
                ),
    );
  }

  /// With the avatar off there is no picture: only the state of the conversation.
  Widget _voiceOnly(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Center(
      child: Semantics(
        label: _phaseText(),
        child: Icon(_phaseIcon(), size: 56, color: c.client.phase == Phase.idle ? scheme.outline : scheme.primary),
      ),
    );
  }

  Widget _statusRow(BuildContext context, {int? maxLines}) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 4, 0),
      child: Row(
        children: [
          Expanded(child: Text(_status(), maxLines: maxLines, overflow: maxLines == null ? null : TextOverflow.ellipsis, style: Theme.of(context).textTheme.bodyMedium)),
          Semantics(
            button: true,
            toggled: c.avatarOn,
            label: c.avatarOn ? Texts.t('avatar.toggle.on') : Texts.t('avatar.toggle.off'),
            child: IconButton(
              tooltip: c.avatarOn ? Texts.t('avatar.toggle.on') : Texts.t('avatar.toggle.off'),
              constraints: const BoxConstraints(minWidth: 48, minHeight: 48),
              onPressed: () => c.setAvatarEnabled(!c.avatarOn),
              icon: Icon(c.avatarOn ? Icons.face : Icons.face_retouching_off),
            ),
          ),
        ],
      ),
    );
  }

  /// The lines under the status: what the phone knows is wrong or missing, and the continuation the PC offers after an interruption.
  Widget _notes(BuildContext context) {
    final g = c.client;
    final scheme = Theme.of(context).colorScheme;
    final note = _note();
    final problem = c.micDenied ? Texts.t('avatar.mic.denied') : (g.problemCode == 'not_loaded' ? Texts.t('avatar.err.not_loaded') : g.problem);
    final route = c.route.problemText;
    final lines = <Widget>[
      if (note.isNotEmpty) Text(note, style: Theme.of(context).textTheme.bodySmall),
      if (problem != null && problem.isNotEmpty) Text(problem, style: TextStyle(color: scheme.error)),
      if (route != null) Text(route, style: TextStyle(color: scheme.error)),
      if (g.resumeOffer.isNotEmpty) _resume(context),
    ];
    if (lines.isEmpty) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [for (final l in lines) Padding(padding: const EdgeInsets.only(bottom: 6), child: l)]),
    );
  }

  Widget _resume(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(8),
      decoration: BoxDecoration(color: scheme.secondaryContainer, borderRadius: BorderRadius.circular(12), border: Border.all(color: scheme.secondary)),
      child: Row(
        children: [
          Expanded(
            child: Semantics(
              label: Texts.t('avatar.resume', [c.client.resumeOffer]),
              child: Text(Texts.t('avatar.resume', [c.client.resumeOffer]), maxLines: 4, overflow: TextOverflow.ellipsis, style: TextStyle(color: scheme.onSecondaryContainer)),
            ),
          ),
          TextButton(onPressed: () => c.answerResume(true), style: TextButton.styleFrom(minimumSize: const Size(48, 48)), child: Text(Texts.t('avatar.resume.yes'))),
          IconButton(tooltip: Texts.t('avatar.resume.no'), constraints: const BoxConstraints(minWidth: 48, minHeight: 48), onPressed: () => c.answerResume(false), icon: const Icon(Icons.close)),
        ],
      ),
    );
  }

  Widget _accordionHeader(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      button: true,
      expanded: chatOpen,
      label: Texts.t('avatar.chat'),
      excludeSemantics: true,
      onTap: () => setState(() => chatOpen = !chatOpen),
      child: InkWell(
        onTap: () => setState(() => chatOpen = !chatOpen),
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
            child: Row(
              children: [
                Icon(Icons.chat_bubble_outline, color: scheme.onSurface),
                const SizedBox(width: 12),
                Expanded(child: Text(Texts.t('avatar.chat'), style: const TextStyle(fontWeight: FontWeight.w600))),
                Icon(chatOpen ? Icons.expand_less : Icons.expand_more),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _lines() {
    final lines = c.client.lines;
    if (lines.isEmpty) {
      return Center(child: Padding(padding: const EdgeInsets.all(16), child: Text(Texts.t('avatar.empty'), textAlign: TextAlign.center)));
    }
    return ListView.builder(
      reverse: true,
      padding: const EdgeInsets.all(8),
      itemCount: lines.length,
      itemBuilder: (context, i) {
        final line = lines[lines.length - 1 - i];
        return Align(
          alignment: line.mine ? Alignment.centerRight : Alignment.centerLeft,
          child: Container(
            margin: const EdgeInsets.symmetric(vertical: 4),
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.8),
            decoration: BoxDecoration(
              color: line.mine ? Theme.of(context).colorScheme.primaryContainer : const Color(0xFF171C21),
              borderRadius: BorderRadius.circular(12),
            ),
            child: SelectableText(line.text, style: const TextStyle(height: 1.5)),
          ),
        );
      },
    );
  }

  /// The conversation text and the field to type in; the field exists only while the accordion is open.
  Widget _chatPanel(BuildContext context) {
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 0, 16, 8),
      decoration: BoxDecoration(color: Theme.of(context).cardColor, borderRadius: BorderRadius.circular(12)),
      clipBehavior: Clip.antiAlias,
      child: Column(
        children: [
          Expanded(child: _lines()),
          Padding(padding: const EdgeInsets.fromLTRB(8, 0, 4, 8), child: _typing()),
        ],
      ),
    );
  }

  Widget _typing() {
    return Row(
      children: [
        Expanded(
          child: TextField(
            controller: typed,
            decoration: InputDecoration(hintText: Texts.t('avatar.type'), border: const OutlineInputBorder()),
            textInputAction: TextInputAction.send,
            onSubmitted: (_) => _sendTyped(),
          ),
        ),
        IconButton(tooltip: Texts.t('avatar.send'), constraints: const BoxConstraints(minWidth: 48, minHeight: 48), onPressed: _sendTyped, icon: const Icon(Icons.send)),
      ],
    );
  }

  void _sendTyped() {
    if (c.sendText(typed.text)) typed.clear();
  }

  // ---- the conversation controls

  /// A round 48 dp button of a group of choices. The chosen one has a thick outline and a filled disc; the others a thin outline: the
  /// state is not in the colour alone.
  Widget _choice({required Key key, required bool selected, required String label, required Widget icon, required VoidCallback onTap, String? value}) {
    final scheme = Theme.of(context).colorScheme;
    return Semantics(
      key: key,
      button: true,
      selected: selected,
      inMutuallyExclusiveGroup: true,
      label: label,
      value: value,
      excludeSemantics: true,
      onTap: onTap,
      child: Tooltip(
        message: label,
        excludeFromSemantics: true,
        child: InkWell(
          customBorder: const CircleBorder(),
          onTap: onTap,
          child: Container(
            width: 48,
            height: 48,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: selected ? scheme.primaryContainer : Colors.transparent,
              border: Border.all(color: selected ? scheme.primary : scheme.outlineVariant, width: selected ? 3 : 1),
            ),
            child: IconTheme(data: IconThemeData(size: 24, color: selected ? scheme.onPrimaryContainer : scheme.onSurface), child: icon),
          ),
        ),
      ),
    );
  }

  Widget _brainChoices() {
    final harness = c.client.brain == 'harness';
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _choice(key: const ValueKey('brain-direct'), selected: !harness, label: Texts.t('avatar.brain.direct.tip'), icon: const LipsIcon(), onTap: () => c.setBrain('direct')),
        const SizedBox(width: 8),
        _choice(key: const ValueKey('brain-harness'), selected: harness, label: Texts.t('avatar.brain.harness.tip'), icon: const Icon(Icons.psychology), onTap: () => c.setBrain('harness')),
      ],
    );
  }

  Widget _routeChoices() {
    final r = c.route;
    String? value(AudioRoute x) => r.selected == x ? Texts.t(r.fullDuplex ? 'route.full' : 'route.half') : null;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _choice(key: const ValueKey('route-bluetooth'), selected: r.selected == AudioRoute.bluetooth, label: Texts.t('route.bluetooth'), value: value(AudioRoute.bluetooth), icon: const Icon(Icons.bluetooth_audio), onTap: () => _pickRoute(AudioRoute.bluetooth)),
        const SizedBox(width: 8),
        _choice(key: const ValueKey('route-speaker'), selected: r.selected == AudioRoute.speaker, label: Texts.t('route.speaker'), value: value(AudioRoute.speaker), icon: const Icon(Icons.volume_up), onTap: () => _pickRoute(AudioRoute.speaker)),
        const SizedBox(width: 8),
        _choice(key: const ValueKey('route-earpiece'), selected: r.selected == AudioRoute.earpiece, label: Texts.t('route.earpiece'), value: value(AudioRoute.earpiece), icon: const Icon(Icons.phone_in_talk), onTap: () => _pickRoute(AudioRoute.earpiece)),
      ],
    );
  }

  /// Bluetooth needs Android's permission, asked here and not at the start, after saying why.
  Future<void> _pickRoute(AudioRoute r) async {
    if (r == AudioRoute.bluetooth && !await c.route.router.hasBluetoothPermission()) {
      if (!mounted) return;
      final go = await showDialog<bool>(
        context: context,
        builder: (d) => AlertDialog(
          title: Text(Texts.t('route.bluetooth')),
          content: Text(Texts.t('route.bt.why')),
          actions: [
            TextButton(onPressed: () => Navigator.pop(d, false), child: Text(Texts.t('route.bt.no'))),
            FilledButton(onPressed: () => Navigator.pop(d, true), child: Text(Texts.t('route.bt.go'))),
          ],
        ),
      );
      if (go != true) return;
    }
    await c.route.select(r);
  }

  Widget _talkButton() {
    final g = c.client;
    final enabled = g.canTalk;
    final scheme = Theme.of(context).colorScheme;
    final speaking = g.phase == Phase.speaking;
    final listening = g.phase == Phase.listening;
    final label = _phaseText();
    final fore = listening ? scheme.onPrimary : scheme.onPrimaryContainer;
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
          constraints: const BoxConstraints(minHeight: 48),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
          alignment: Alignment.center,
          decoration: BoxDecoration(
            color: !enabled ? scheme.surfaceContainerHighest : (listening ? scheme.primary : scheme.primaryContainer),
            borderRadius: BorderRadius.circular(24),
            border: Border.all(color: enabled ? scheme.primary : scheme.outlineVariant, width: listening ? 3 : 1),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(listening ? Icons.mic : Icons.mic_none, color: fore),
              const SizedBox(width: 8),
              Flexible(child: Text(label, overflow: TextOverflow.ellipsis, style: TextStyle(fontWeight: FontWeight.w600, color: fore))),
            ],
          ),
        ),
      ),
    );
  }

  /// The choices of who answers and where the sound goes, and the bar. With `tight` (the chat is open and there is little room, as with the
  /// keyboard up) only the bar is kept, so that the field is never pushed off the screen.
  Widget _controls({required bool tight}) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (!tight) ...[
            Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [_brainChoices(), _routeChoices()]),
            const SizedBox(height: 8),
          ],
          _talkButton(),
        ],
      ),
    );
  }

  Widget _top(BuildContext context, double h, {required bool tight}) {
    if (tight) return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [_accordionHeader(context), Expanded(child: _chatPanel(context))]);
    final cramped = chatOpen && h < 380;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ConstrainedBox(
          constraints: BoxConstraints(maxHeight: max(0, h * 0.45)),
          child: SingleChildScrollView(child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [_statusRow(context, maxLines: cramped ? 1 : null), if (!cramped) _notes(context)])),
        ),
        if (c.avatarOn && !cramped) Expanded(flex: chatOpen ? 2 : 1, child: _stage(context)),
        if (!c.avatarOn && !chatOpen) Expanded(child: _voiceOnly(context)),
        _accordionHeader(context),
        if (chatOpen) Expanded(flex: 5, child: _chatPanel(context)),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: Listenable.merge([c, c.route]),
      builder: (context, _) {
        return Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 640),
            child: LayoutBuilder(builder: (context, box) {
              final tight = chatOpen && box.maxHeight < 440;
              return Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Expanded(child: LayoutBuilder(builder: (context, top) => _top(context, top.maxHeight, tight: tight))),
                  _controls(tight: tight),
                ],
              );
            }),
          ),
        );
      },
    );
  }
}
