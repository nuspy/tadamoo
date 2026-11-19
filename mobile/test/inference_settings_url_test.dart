import 'package:basalt_mobile/src/inference_settings_screen.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('the key goes once as a query parameter, the fragment opens the chat view', () {
    final url = inferenceSettingsUrl(Uri.parse('http://192.168.1.10:7232'), 'abc123');
    expect(url, 'http://192.168.1.10:7232?key=abc123#chat');
  });

  test('a base address with its own query is kept, the key is added beside it', () {
    final url = inferenceSettingsUrl(Uri.parse('https://example.tailnet.ts.net:7232/?lang=it'), 's3cret');
    expect(url, contains('lang=it'));
    expect(url, contains('key=s3cret'));
    expect(url, endsWith('#chat'));
  });
}
