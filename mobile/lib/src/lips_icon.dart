import 'package:flutter/widgets.dart';

/// A pair of lips, for the Direct brain (Material has none). It takes the size and the colour of the icon theme, like an `Icon`.
class LipsIcon extends StatelessWidget {
  const LipsIcon({super.key, this.size, this.color});
  final double? size;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final theme = IconTheme.of(context);
    final s = size ?? theme.size ?? 24;
    return ExcludeSemantics(
      child: SizedBox(width: s, height: s, child: CustomPaint(painter: _LipsPainter(color ?? theme.color ?? const Color(0xFFFFFFFF)))),
    );
  }
}

class _LipsPainter extends CustomPainter {
  _LipsPainter(this.color);
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;
    Offset p(double x, double y) => Offset(w * x, h * y);
    final upper = Path()
      ..moveTo(p(0.06, 0.52).dx, p(0.06, 0.52).dy)
      ..cubicTo(w * 0.22, h * 0.40, w * 0.34, h * 0.24, w * 0.50, h * 0.34)
      ..cubicTo(w * 0.66, h * 0.24, w * 0.78, h * 0.40, w * 0.94, h * 0.52)
      ..cubicTo(w * 0.78, h * 0.56, w * 0.66, h * 0.58, w * 0.50, h * 0.58)
      ..cubicTo(w * 0.34, h * 0.58, w * 0.22, h * 0.56, w * 0.06, h * 0.52)
      ..close();
    final lower = Path()
      ..moveTo(p(0.06, 0.52).dx, p(0.06, 0.52).dy)
      ..cubicTo(w * 0.22, h * 0.56, w * 0.34, h * 0.58, w * 0.50, h * 0.58)
      ..cubicTo(w * 0.66, h * 0.58, w * 0.78, h * 0.56, w * 0.94, h * 0.52)
      ..cubicTo(w * 0.84, h * 0.76, w * 0.66, h * 0.80, w * 0.50, h * 0.80)
      ..cubicTo(w * 0.34, h * 0.80, w * 0.16, h * 0.76, w * 0.06, h * 0.52)
      ..close();
    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = w * 0.075
      ..strokeJoin = StrokeJoin.round;
    final fill = Paint()
      ..color = color.withValues(alpha: 0.35)
      ..style = PaintingStyle.fill;
    canvas.drawPath(upper, fill);
    canvas.drawPath(lower, fill);
    canvas.drawPath(upper, stroke);
    canvas.drawPath(lower, stroke);
  }

  @override
  bool shouldRepaint(_LipsPainter old) => old.color != color;
}
