import 'package:basalt_mobile/src/chat_model.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('a new conversation sends the fields the PC page sends', () {
    final b = NewChat(mode: 'auto', effort: 'low', allow: ['shell_command:git *'], worktree: 'always').body();
    expect(b, {'mode': 'auto', 'allow': ['shell_command:git *'], 'ask_only': false, 'effort': 'low', 'worktree': 'always'});
  });

  test('the defaults are the ones the app always used', () {
    expect(NewChat().body(), {'mode': 'default', 'allow': <String>[], 'ask_only': false, 'effort': '', 'worktree': 'auto'});
    expect(NewChat().copy(mode: 'plan').mode, 'plan');
  });
}
