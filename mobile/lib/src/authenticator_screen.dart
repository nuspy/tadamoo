import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import 'api.dart';
import 'i18n.dart';

/// What the camera has read so far and what was handed to the PC; the PC keeps the links and the person confirms there, so nothing of the vault is changed from the phone.
class OfferTracker {
  OfferTracker(this.send);

  /// Hands one link to the PC and returns its progress (`parts`, `have`, `accounts`, `complete`).
  final Future<Map<String, dynamic>> Function(String link) send;
  final Set<String> seen = {};
  Map<String, dynamic> progress = const {};
  String? error;

  bool get complete => progress['complete'] == true;

  /// A QR text of an authenticator export or account is handed over once; anything else is ignored. Returns whether the state changed.
  Future<bool> offer(String text) async {
    final t = text.trim();
    if (!t.toLowerCase().startsWith('otpauth') || !seen.add(t)) return false;
    try {
      progress = Map<String, dynamic>.from(await send(t));
      error = null;
    } on ApiError catch (e) {
      error = e.message;
      seen.remove(t);
    }
    return true;
  }

  String line() {
    if (error != null) return error!;
    final parts = (progress['parts'] as num?)?.toInt() ?? 0;
    final have = ((progress['have'] as List?) ?? const []).length;
    if (progress['mixed'] == true) return Texts.t('authn.mixed');
    if (complete) return Texts.t('authn.done', [(progress['accounts'] as num?)?.toInt() ?? 0]);
    if (parts > 0) return Texts.t('authn.parts', [have, parts]);
    return Texts.t('authn.aim');
  }
}

/// Reads the QR codes of Google Authenticator's export with the camera and hands them to the PC, where the person chooses the sites and confirms.
class AuthenticatorScanScreen extends StatefulWidget {
  const AuthenticatorScanScreen({super.key, required this.api});
  final Api api;
  @override
  State<AuthenticatorScanScreen> createState() => _AuthenticatorScanScreenState();
}

class _AuthenticatorScanScreenState extends State<AuthenticatorScanScreen> {
  late final OfferTracker tracker = OfferTracker((link) => widget.api.post('/api/authenticator/offer', {'text': link}).then((r) => Map<String, dynamic>.from((r['progress'] as Map?) ?? const {})));
  bool busy = false;

  Future<void> _detected(BarcodeCapture capture) async {
    if (busy) return;
    busy = true;
    var changed = false;
    for (final b in capture.barcodes) {
      final v = b.rawValue;
      if (v != null && await tracker.offer(v)) changed = true;
    }
    busy = false;
    if (changed && mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: Text(Texts.t('authn.title'))),
      body: Column(children: [
        Expanded(child: tracker.complete ? const Center(child: Icon(Icons.check_circle_outline, size: 72)) : MobileScanner(onDetect: _detected)),
        Padding(
          padding: const EdgeInsets.all(16),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Text(tracker.line(), key: const Key('authn-line'), textAlign: TextAlign.center, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(Texts.t(tracker.complete ? 'authn.confirm' : 'authn.help'), textAlign: TextAlign.center),
            const SizedBox(height: 12),
            SizedBox(width: double.infinity, height: 48, child: FilledButton(onPressed: () => Navigator.of(context).pop(), child: Text(Texts.t(tracker.complete ? 'authn.close' : 'authn.cancel')))),
          ]),
        ),
      ]),
    );
  }
}
