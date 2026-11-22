import 'package:flutter/material.dart';

import 'i18n.dart';

/// How present pictures and charts may be, from the least to the most (the values the server accepts).
const visualLevels = ['very_low', 'low', 'normal', 'high', 'very_high', 'highest'];

/// The level a switch starts at when it is turned on.
const defaultVisualLevel = 'normal';

/// The two switches of a conversation, "pictures from the web" and "charts and diagrams that explain", each with how present it is.
/// A level of '' is off. `onChanged` gets both values and returns the server's complaint, or null when the change was made.
class VisualsBox extends StatefulWidget {
  const VisualsBox({super.key, required this.images, required this.charts, required this.onChanged});
  final String images;
  final String charts;
  final Future<String?> Function(String images, String charts) onChanged;
  @override
  State<VisualsBox> createState() => _VisualsBoxState();
}

class _VisualsBoxState extends State<VisualsBox> {
  late String images = widget.images;
  late String charts = widget.charts;
  String problem = '';
  bool busy = false;

  Future<void> _set(String newImages, String newCharts) async {
    setState(() {
      busy = true;
      problem = '';
    });
    final complaint = await widget.onChanged(newImages, newCharts);
    if (!mounted) return;
    setState(() {
      busy = false;
      if (complaint == null) {
        images = newImages;
        charts = newCharts;
      } else {
        problem = complaint;
      }
    });
  }

  Widget _row(String key, String label, String level, void Function(String) change) {
    final on = level.isNotEmpty;
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      CheckboxListTile(
        key: ValueKey('vis-$key'),
        contentPadding: EdgeInsets.zero,
        controlAffinity: ListTileControlAffinity.leading,
        value: on,
        onChanged: busy ? null : (v) => change(v == true ? defaultVisualLevel : ''),
        title: Text(label),
      ),
      Padding(
        padding: const EdgeInsets.only(left: 40, bottom: 8),
        child: DropdownButtonFormField<String>(
          key: ValueKey('vis-$key-level-$level'),
          initialValue: on ? level : defaultVisualLevel,
          isExpanded: true,
          decoration: InputDecoration(labelText: Texts.t('vis.level'), border: const OutlineInputBorder(), isDense: false),
          items: [for (final l in visualLevels) DropdownMenuItem(value: l, child: Text(Texts.t('vis.level.$l')))],
          onChanged: on && !busy ? (v) => change(v ?? level) : null,
        ),
      ),
    ]);
  }

  @override
  Widget build(BuildContext context) {
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      _row('img', Texts.t('vis.img'), images, (l) => _set(l, charts)),
      _row('chart', Texts.t('vis.chart'), charts, (l) => _set(images, l)),
      if (problem.isNotEmpty) Semantics(liveRegion: true, child: Text(problem, style: TextStyle(color: Theme.of(context).colorScheme.error))),
    ]);
  }
}
