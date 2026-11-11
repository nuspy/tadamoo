import 'package:flutter/material.dart';

import 'i18n.dart';

/// One personality the PC offers: avatars have a face and a voice, colleagues are for the work (critical, analytical, exact...).
class PersonaChoice {
  PersonaChoice(this.slug, this.name, this.avatar);
  final String slug;
  final String name;
  final bool avatar;

  static List<PersonaChoice> parse(Object? list) => [
        for (final p in (list as List? ?? const []).whereType<Map>())
          if ('${p['slug']}'.isNotEmpty) PersonaChoice('${p['slug']}', '${p['name'] ?? p['slug']}', p['kind'] == 'avatar'),
      ];
}

/// A small menu in the bar of a conversation: which personality or colleague answers. One icon; the name is in its tip.
class PersonaMenu extends StatelessWidget {
  const PersonaMenu({super.key, required this.choices, required this.current, required this.onPick});
  final List<PersonaChoice> choices;
  final String current;
  final ValueChanged<String> onPick;

  @override
  Widget build(BuildContext context) {
    if (choices.isEmpty && current.isEmpty) return const SizedBox.shrink();
    final known = choices.where((c) => c.slug == current);
    final now = known.isNotEmpty ? known.first.name : current;
    final groups = [
      (Texts.t('persona.avatars'), choices.where((c) => c.avatar).toList()),
      (Texts.t('persona.colleagues'), choices.where((c) => !c.avatar).toList()),
    ].where((g) => g.$2.isNotEmpty).toList();
    PopupMenuItem<String> row(String slug, String text) => PopupMenuItem<String>(
          value: slug,
          height: 44,
          child: Row(children: [
            SizedBox(width: 24, child: slug == current ? const Icon(Icons.check, size: 18) : null),
            Flexible(child: Text(text, overflow: TextOverflow.ellipsis)),
          ]),
        );
    return PopupMenuButton<String>(
      key: const ValueKey('persona-menu'),
      tooltip: current.isEmpty ? Texts.t('persona.tip') : Texts.t('persona.tip.on', [now]),
      icon: Icon(current.isEmpty ? Icons.person_outline : Icons.person),
      onSelected: onPick,
      itemBuilder: (c) => [
        row('', Texts.t('persona.none')),
        for (final g in groups) ...[
          if (groups.length > 1) PopupMenuItem<String>(enabled: false, height: 32, child: Text(g.$1, style: Theme.of(c).textTheme.labelSmall)),
          for (final p in g.$2) row(p.slug, p.name),
        ],
      ],
    );
  }
}
