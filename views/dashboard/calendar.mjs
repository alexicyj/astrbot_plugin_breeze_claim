export function monthGrid(today, offset = 0) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(today)) throw new Error('日期格式无效');
  const [year, month, day] = today.split('-').map(Number);
  const checked = new Date(Date.UTC(year, month - 1, day));
  if (checked.toISOString().slice(0, 10) !== today) throw new Error('日期无效');
  const first = new Date(Date.UTC(year, month - 1 + offset, 1));
  const start = (first.getUTCDay() + 6) % 7;
  const days = Array.from({ length: 42 }, (_, index) => {
    const date = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), 1 - start + index));
    const key = date.toISOString().slice(0, 10);
    return { key, day: date.getUTCDate(), inMonth: date.getUTCMonth() === first.getUTCMonth(), isToday: key === today };
  });
  return {
    label: new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', timeZone: 'UTC' }).format(first),
    monthKey: first.toISOString().slice(0, 7),
    days,
  };
}
