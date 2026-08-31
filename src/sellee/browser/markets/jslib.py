"""JS every marketplace reader needs, written once.

An adapter module holds what one marketplace does differently. This holds the opposite: the small
pieces that are the same wherever they are used, so a second market cannot arrive with a subtly
different version of them. There is exactly one entry here today and it earns its own module by
being the one piece of adapter JS with a test of its own.
"""

from __future__ import annotations

# A price, from rendered text, as a number.
#
# The thousands separator is not the same on every regional site, so neither `,` nor `.` can be
# assumed to be the decimal point: Carousell's Indonesian site writes 1.500.000 where its Singapore
# site writes 1,500,000, and reading either wrong is off by a factor of a thousand on the seller's
# own asking price.
PARSE_PRICE_JS = """(text) => {
  const trimmed = String(text || '').replace(/[^0-9.,]/g, '');
  if (!trimmed) return NaN;
  const lastDot = trimmed.lastIndexOf('.');
  const lastComma = trimmed.lastIndexOf(',');
  if (lastDot >= 0 && lastComma >= 0) {
    // Both appear: the later is the decimal point, the other groups thousands.
    const decimal = lastDot > lastComma ? '.' : ',';
    const grouping = decimal === '.' ? ',' : '.';
    return Number(trimmed.split(grouping).join('').replace(decimal, '.'));
  }
  const sep = lastDot >= 0 ? '.' : (lastComma >= 0 ? ',' : '');
  if (!sep) return Number(trimmed);
  // One separator, so its job is inferred: repeated, or three digits after the last one, groups
  // thousands ("1.500.000", "1,299"); anything else is a decimal point ("40.00", "1,5").
  const parts = trimmed.split(sep);
  const groups = parts.length > 2 || parts[parts.length - 1].length === 3;
  return Number(groups ? parts.join('') : parts.join('.'));
}"""
