import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'audio_route.dart';
import 'avatar_assets.dart';
import 'avatar_engines.dart';
import 'avatar_hub.dart';
import 'i18n.dart';
import 'settings.dart';

/// The "Avatar and voice" part of Settings: avatar on or off, the animation system, the picture's size and quality, the personality, who
/// answers by default, where the sound goes and the audio test. Everything is saved on the phone; the Avatar tab sends what the PC must
/// know (the personality, who answers, the video hints) when it is connected.
class AvatarVoiceSettings extends StatefulWidget {
  const AvatarVoiceSettings({super.key, required this.settings, required this.route, required this.hub, required this.loadPersonas});
  final AppSettings settings;
  final AudioRouteController route;

  /// What this phone can draw: the avatar's files, the model, the self-test.
  final EngineHub hub;

  /// The PC's personalities as `{slug, name}`; may fail or be empty (the PC's own personality stays available).
  final Future<List<Map<String, String>>> Function() loadPersonas;

  @override
  State<AvatarVoiceSettings> createState() => _AvatarVoiceSettingsState();
}

class _AvatarVoiceSettingsState extends State<AvatarVoiceSettings> {
  List<Map<String, String>> personas = const [];

  AppSettings get s => widget.settings;
  AudioRouteController get route => widget.route;
  EngineHub get hub => widget.hub;

  @override
  void initState() {
    super.initState();
    widget.loadPersonas().then((list) {
      if (mounted) setState(() => personas = list);
    }).catchError((_) {});
  }

  /// A choice of a group: a radio mark (a different shape when chosen), the name and lines of detail. At least 48 dp high.
  Widget _option({required bool selected, required bool enabled, required String title, required List<String> lines, required VoidCallback onTap, Key? key}) {
    final scheme = Theme.of(context).colorScheme;
    final text = Theme.of(context).textTheme;
    return Semantics(
      key: key,
      button: true,
      selected: selected,
      enabled: enabled,
      inMutuallyExclusiveGroup: true,
      label: [title, ...lines].join('. '),
      excludeSemantics: true,
      onTap: enabled ? onTap : null,
      child: InkWell(
        onTap: enabled ? onTap : null,
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 48),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.only(top: 2, right: 12),
                  child: Icon(!enabled ? Icons.block : (selected ? Icons.radio_button_checked : Icons.radio_button_unchecked), color: enabled ? (selected ? scheme.primary : scheme.onSurface) : scheme.onSurfaceVariant),
                ),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(title, style: text.bodyLarge?.copyWith(fontWeight: selected ? FontWeight.w700 : FontWeight.w500, color: enabled ? null : scheme.onSurfaceVariant)),
                      for (final l in lines) Text(l, style: text.bodySmall?.copyWith(color: scheme.onSurfaceVariant)),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }

  /// The honest line of an engine: what it needs on this phone, or that it is ready.
  String _status(AvatarEngine e) {
    final st = hub.status(e);
    final store = hub.store;
    switch (st) {
      case EngineStatus.ready:
        if (e.id == 'wav2lip' && s.wav2lipFps > 0) return Texts.t('av.st.ready.fps', [s.wav2lipFps.toStringAsFixed(1)]);
        return Texts.t('av.st.ready');
      case EngineStatus.needsAssets:
        return hub.avatarName.isEmpty ? Texts.t('av.st.assets.open') : Texts.t('av.st.assets');
      case EngineStatus.needsModel:
        return store != null && store.modelStage == ModelStage.failed ? Texts.t('av.st.model.failed', [store.modelReason]) : Texts.t('av.st.model');
      case EngineStatus.downloading:
        if (e.id == 'wav2lip' && store != null) return Texts.t('av.st.downloading', ['${(store.modelGot * 100 ~/ (store.modelTotal == 0 ? 1 : store.modelTotal))} %']);
        return Texts.t('av.st.downloading', ['']);
      case EngineStatus.notTested:
        return Texts.t('av.st.nottested');
      case EngineStatus.testing:
        return Texts.t('av.st.testing');
      case EngineStatus.failed:
        return Texts.t('av.st.failed', [hub.lastTest?.reason ?? '']);
      case EngineStatus.noRuntime:
        return Texts.t('av.st.noruntime');
      case EngineStatus.unavailable:
        return Texts.t('av.engine.na');
    }
  }

  /// The weight line: the facts known (the model's size, the assets on the phone), or that it is not measured.
  String _weight(AvatarEngine e) {
    if (e.id == 'visemes') {
      final bytes = hub.assetBytes();
      return bytes > 0 ? Texts.t('eng.visemes.w.ready', [(bytes / 1024).round()]) : Texts.t(e.weightKey);
    }
    return Texts.t(e.weightKey);
  }

  /// The button that gets what an engine needs, when it can be got from here.
  Widget? _fetchButton(AvatarEngine e) {
    final st = hub.status(e);
    if (e.id == 'visemes' && st == EngineStatus.needsAssets && hub.avatarName.isNotEmpty) {
      return TextButton.icon(
        key: const ValueKey('engine-visemes-fetch'),
        style: TextButton.styleFrom(minimumSize: const Size(48, 48)),
        onPressed: () => hub.fetchAssets(hub.avatarName),
        icon: const Icon(Icons.download),
        label: Text(Texts.t('av.dl.assets')),
      );
    }
    if (e.id == 'wav2lip') {
      if (st == EngineStatus.needsModel) {
        return TextButton.icon(
          key: const ValueKey('engine-wav2lip-fetch'),
          style: TextButton.styleFrom(minimumSize: const Size(48, 48)),
          onPressed: hub.downloadModel,
          icon: const Icon(Icons.download),
          label: Text(Texts.t('av.dl.model')),
        );
      }
      if (st == EngineStatus.needsAssets && hub.avatarName.isNotEmpty) {
        return TextButton.icon(
          key: const ValueKey('engine-wav2lip-fetch'),
          style: TextButton.styleFrom(minimumSize: const Size(48, 48)),
          onPressed: () => hub.fetchAssets(hub.avatarName),
          icon: const Icon(Icons.download),
          label: Text(Texts.t('av.dl.assets')),
        );
      }
    }
    return null;
  }

  Widget _engines() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final e in avatarEngines) ...[
          _option(
            key: ValueKey('engine-${e.id}'),
            selected: e.selectable && s.avatarMode == e.id,
            enabled: e.selectable,
            title: Texts.t(e.nameKey),
            lines: [_weight(e), Texts.t(e.noteKey), if (e.id != 'video') _status(e)],
            onTap: () => s.setAvatarMode(e.id),
          ),
          if (_fetchButton(e) case final button?) Align(alignment: Alignment.centerLeft, child: button),
        ],
      ],
    );
  }

  Widget _avatarTest() {
    final r = hub.lastTest;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(Texts.t('av.atest.help'), style: Theme.of(context).textTheme.bodySmall),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton.icon(
            key: const ValueKey('avatar-test'),
            style: OutlinedButton.styleFrom(minimumSize: const Size(48, 48)),
            onPressed: hub.testing ? null : hub.runTest,
            icon: hub.testing ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.speed),
            label: Text(hub.testing ? Texts.t('av.atest.running') : Texts.t('av.atest')),
          ),
        ),
        if (r != null) ...[
          const SizedBox(height: 8),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(color: Theme.of(context).cardColor, borderRadius: BorderRadius.circular(8)),
            child: SelectableText(r.toText(), key: const ValueKey('avatar-test-report'), style: const TextStyle(fontFamily: 'monospace', fontSize: 12, height: 1.4)),
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              key: const ValueKey('avatar-test-copy'),
              style: TextButton.styleFrom(minimumSize: const Size(48, 48)),
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: r.toText()));
                if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(Texts.t('av.test.copied'))));
              },
              icon: const Icon(Icons.copy),
              label: Text(Texts.t('av.test.copy')),
            ),
          ),
        ],
      ],
    );
  }

  Widget _persona() {
    final slugs = personas.map((p) => p['slug']).toSet();
    final saved = s.avatarPersona;
    return DropdownButtonFormField<String>(
      key: ValueKey('persona-$saved-${personas.length}'),
      initialValue: saved,
      isExpanded: true,
      decoration: InputDecoration(labelText: Texts.t('avatar.persona'), helperText: Texts.t('av.persona.help'), helperMaxLines: 3, border: const OutlineInputBorder()),
      items: [
        DropdownMenuItem(value: '', child: Text(Texts.t('avatar.persona.default'))),
        if (saved.isNotEmpty && !slugs.contains(saved)) DropdownMenuItem(value: saved, child: Text(saved, overflow: TextOverflow.ellipsis)),
        for (final p in personas) DropdownMenuItem(value: p['slug']!, child: Text(p['name']!, overflow: TextOverflow.ellipsis)),
      ],
      onChanged: (v) => s.setAvatarPersona(v ?? ''),
    );
  }

  Widget _brain() {
    final harness = s.avatarBrain == 'harness';
    return SegmentedButton<String>(
      style: const ButtonStyle(minimumSize: WidgetStatePropertyAll(Size(48, 48))),
      segments: [
        ButtonSegment(value: 'direct', label: Text(Texts.t('avatar.brain.direct')), tooltip: Texts.t('avatar.brain.direct.tip')),
        ButtonSegment(value: 'harness', label: Text(Texts.t('avatar.brain.harness')), tooltip: Texts.t('avatar.brain.harness.tip')),
      ],
      selected: {harness ? 'harness' : 'direct'},
      onSelectionChanged: (v) => s.setAvatarBrain(v.first),
    );
  }

  Widget _routes() {
    String duplex(AudioRoute r) => Texts.t(r == AudioRoute.bluetooth ? 'route.full' : (r == AudioRoute.speaker ? (s.fullDuplexSpeaker && s.audioEchoCancel == true ? 'route.full' : 'route.half') : 'route.half'));
    final names = {AudioRoute.bluetooth: 'route.bluetooth', AudioRoute.speaker: 'route.speaker', AudioRoute.earpiece: 'route.earpiece'};
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final r in AudioRoute.values)
          _option(
            key: ValueKey('setroute-${r.name}'),
            selected: route.selected == r,
            enabled: true,
            title: Texts.t(names[r]!),
            lines: [duplex(r)],
            onTap: () => route.select(r),
          ),
        if (route.problemText != null) Padding(padding: const EdgeInsets.only(top: 4), child: Text(route.problemText!, style: TextStyle(color: Theme.of(context).colorScheme.error))),
      ],
    );
  }

  Widget _audioTest() {
    final report = route.report;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(Texts.t('av.test.help'), style: Theme.of(context).textTheme.bodySmall),
        const SizedBox(height: 8),
        Align(
          alignment: Alignment.centerLeft,
          child: OutlinedButton.icon(
            style: OutlinedButton.styleFrom(minimumSize: const Size(48, 48)),
            onPressed: route.testing ? null : route.runTest,
            icon: route.testing ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.graphic_eq),
            label: Text(route.testing ? Texts.t('av.test.running') : Texts.t('av.test')),
          ),
        ),
        if (report != null) ...[
          const SizedBox(height: 8),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(color: Theme.of(context).cardColor, borderRadius: BorderRadius.circular(8)),
            child: SelectableText(report.toText(), key: const ValueKey('audio-report'), style: const TextStyle(fontFamily: 'monospace', fontSize: 12, height: 1.4)),
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton.icon(
              style: TextButton.styleFrom(minimumSize: const Size(48, 48)),
              onPressed: () async {
                await Clipboard.setData(ClipboardData(text: report.toText()));
                if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(Texts.t('av.test.copied'))));
              },
              icon: const Icon(Icons.copy),
              label: Text(Texts.t('av.test.copy')),
            ),
          ),
        ],
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return ListenableBuilder(
      listenable: Listenable.merge([s, route, hub, if (hub.store != null) hub.store!]),
      builder: (context, _) {
        final quality = s.avatarQuality;
        final aec = s.audioEchoCancel == true;
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SwitchListTile(
              key: const ValueKey('avatar-enabled'),
              contentPadding: EdgeInsets.zero,
              isThreeLine: true,
              title: Text(Texts.t('av.on')),
              subtitle: Text(Texts.t('av.on.help')),
              value: s.avatarEnabled,
              onChanged: s.setAvatarEnabled,
            ),
            const SizedBox(height: 8),
            Text(Texts.t('av.engine'), style: theme.textTheme.titleSmall),
            Text(Texts.t('av.engine.help'), style: theme.textTheme.bodySmall),
            const SizedBox(height: 4),
            _engines(),
            const SizedBox(height: 8),
            Text(Texts.t('av.atest'), style: theme.textTheme.titleSmall),
            _avatarTest(),
            const SizedBox(height: 12),
            DropdownButtonFormField<int>(
              key: ValueKey('res-${s.avatarResolution}'),
              initialValue: s.avatarResolution,
              isExpanded: true,
              decoration: InputDecoration(labelText: Texts.t('av.res'), helperText: Texts.t('av.res.help'), helperMaxLines: 5, border: const OutlineInputBorder()),
              items: [for (final r in avatarResolutions) DropdownMenuItem(value: r, child: Text(Texts.t('av.res.px', [r])))],
              onChanged: (v) => v == null ? null : s.setAvatarResolution(v),
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<AvatarQuality>(
              key: ValueKey('quality-${quality.name}'),
              initialValue: quality,
              isExpanded: true,
              decoration: InputDecoration(labelText: Texts.t('av.q'), helperText: Texts.t('av.q.help', [quality.fps, quality.bitrateKbps]), helperMaxLines: 3, border: const OutlineInputBorder()),
              items: [for (final q in AvatarQuality.values) DropdownMenuItem(value: q, child: Text(Texts.t('av.q.${q.name}')))],
              onChanged: (v) => v == null ? null : s.setAvatarQuality(v),
            ),
            const SizedBox(height: 12),
            _persona(),
            const SizedBox(height: 12),
            Text(Texts.t('av.brain'), style: theme.textTheme.titleSmall),
            const SizedBox(height: 4),
            Text(Texts.t('avatar.brain.help'), style: theme.textTheme.bodySmall),
            const SizedBox(height: 8),
            _brain(),
            const SizedBox(height: 16),
            Text(Texts.t('av.audio'), style: theme.textTheme.titleSmall),
            const SizedBox(height: 4),
            Text(Texts.t('av.route'), style: theme.textTheme.bodySmall),
            _routes(),
            SwitchListTile(
              key: const ValueKey('full-duplex'),
              contentPadding: EdgeInsets.zero,
              isThreeLine: true,
              title: Text(Texts.t('av.duplex')),
              subtitle: Text(aec ? Texts.t('av.duplex.help') : '${Texts.t('av.duplex.need')} ${Texts.t('av.duplex.help')}'),
              value: s.fullDuplexSpeaker && aec,
              onChanged: aec ? s.setFullDuplexSpeaker : null,
            ),
            _audioTest(),
          ],
        );
      },
    );
  }
}
