import 'package:flutter/material.dart';

import 'api.dart';
import 'i18n.dart';

/// Changing the server's key from the phone: the PC applies the new key at once (`POST /api/server-key`, serve/server_key.py); the phone then keeps the new key as its own.
extension ServerKeyApi on Api {
  Future<void> changeServerKey(String current, String next) => post('/api/server-key', {'current': current, 'new': next});
}

/// The current key, the new one and the new one again. The answer is (current, new), or null when the person gave up.
class KeyChangeDialog extends StatefulWidget {
  const KeyChangeDialog({super.key, required this.current});
  final String current;
  @override
  State<KeyChangeDialog> createState() => _KeyChangeDialogState();
}

class _KeyChangeDialogState extends State<KeyChangeDialog> {
  late final TextEditingController current = TextEditingController(text: widget.current);
  final next = TextEditingController();
  final again = TextEditingController();
  String? error;

  @override
  void dispose() {
    current.dispose();
    next.dispose();
    again.dispose();
    super.dispose();
  }

  void _ok() {
    final n = next.text.trim();
    if (n.isEmpty) return setState(() => error = Texts.t('sk.empty'));
    if (n != again.text.trim()) return setState(() => error = Texts.t('sk.mismatch'));
    Navigator.of(context).pop((current.text.trim(), n));
  }

  @override
  Widget build(BuildContext context) {
    Widget field(TextEditingController c, String label, {Key? key}) => Padding(
          padding: const EdgeInsets.only(bottom: 10),
          child: TextField(key: key, controller: c, obscureText: true, autocorrect: false, enableSuggestions: false, decoration: InputDecoration(labelText: label, border: const OutlineInputBorder())),
        );
    return AlertDialog(
      title: Text(Texts.t('sk.title')),
      content: SingleChildScrollView(
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(Texts.t('sk.help'), style: Theme.of(context).textTheme.bodySmall),
          const SizedBox(height: 12),
          field(current, Texts.t('sk.current'), key: const ValueKey('sk-current')),
          field(next, Texts.t('sk.new'), key: const ValueKey('sk-new')),
          field(again, Texts.t('sk.again'), key: const ValueKey('sk-again')),
          if (error != null) Text(error!, style: const TextStyle(color: Colors.redAccent)),
        ]),
      ),
      actions: [
        TextButton(style: TextButton.styleFrom(minimumSize: const Size(48, 48)), onPressed: () => Navigator.of(context).pop(), child: Text(Texts.t('sk.cancel'))),
        FilledButton(style: FilledButton.styleFrom(minimumSize: const Size(48, 48)), onPressed: _ok, child: Text(Texts.t('sk.change'))),
      ],
    );
  }
}
