/** Joins class names, skipping falsy entries: cx('a', cond && 'b'). */
export function cx(...classes: (string | false | null | undefined)[]): string {
  return classes.filter(Boolean).join(' ')
}
