/**
 * 把"多少字"说成人能一眼估出规模的写法。
 *
 * 为什么不用"条"来报损失：条数是消息数，不是信息量。一条三万字文档被整条裁掉
 * 也只算 1 条——用户看到"省略 1 条"会以为丢的是一句寒暄。数字必须指向真实损失，
 * 否则这句提示比不说更误导。
 *
 * 一万以下直接给数字（3200 比"3.2 千"更好读）；一万以上用"万"，因为中文里
 * "3.2 万字"是日常说法，"32000 字"要停下来数位数。
 */
export function humanChars(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0'
  if (n < 10000) return String(Math.round(n))
  const wan = n / 10000
  // 十万以上不再保留小数：12.3 万不如 12 万，那时候精度已经没意义了。
  const text = wan >= 10 ? String(Math.round(wan)) : wan.toFixed(1).replace(/\.0$/, '')
  return `${text} 万`
}
