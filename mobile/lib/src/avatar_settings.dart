import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'audio_route.dart';
import 'avatar_engines.dart';
import 'i18n.dart';
import 'settings.dart';

/// The "Avatar and voice" part of Settings: avatar on or off, the animation system, the picture's size and quality, the personality, who
/// answers by default, where the sound goes and the audio test. Everything is saved on the phone; the Avatar tab sends what the PC must
/// know (the personality, who answers, the video hints) when it is connected.
class AvatarVoiceSettings extends StatefulWidget {
  const AvatarVoiceSettings({super.key, required this.settings, required this.route, required this.loadPersonas});
  final AppSettings settings;
  final AudioRouteController route;

  /// The PC's personalities as `{slug, name}`; may fail or be empty (the PC's own personality stays available).
  final Future<List<Map<String, String>>> Function() loadPersonas;

  @override
  State<AvatarVoiceSettings> createState() => _AvatarVoiceSettingsState();
}

class _AvatarVoiceSettingsState extends State<AvatarVoiceSettings> {
  List<Map<String, String>> personas = const [];

  AppSettings get s => widget.settings;
  AudioRouteController get route => widget.route;

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

  Widget _engines() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final e in avatarEngines)
          _option(
            key: ValueKey('engine-${e.id}'),
            selected: e.selectable && s.avatarMode == e.id,
            enabled: e.selectable,
            title: Texts.t(e.nameKey),
            lines: [
              Texts.t(e.weightKey),
              Texts.t(e.noteKey),
              if (!e.selectable) Texts.t('av.engine.na') else if (s.avatarMode == e.id && !e.hasRuntime) Texts.t('av.engine.later'),
            ],
            onTap: () => s.setAvatarMode(e.id),
          ),
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
      listenable: Listenable.merge([s, route]),
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
