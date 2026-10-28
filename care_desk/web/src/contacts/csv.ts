// Reads an old-contacts CSV into rows keyed by the desk's column names.
// Quoted cells may hold commas, newlines and doubled quotes.

const ALIAS: Record<string, string> = {
  name: 'name', full_name: 'name', email: 'email', email_address: 'email', interest: 'interest', service: 'interest',
  last_contact_days: 'last_contact_days', days_since_contact: 'last_contact_days', last_contact: 'last_contact_days', days: 'last_contact_days',
  consent: 'consent', email_consent: 'consent', opt_in: 'consent', opted_in: 'consent',
};

export function parseCSV(text: string): Record<string, string>[] {
  const rows: string[][] = [];
  let row: string[] = [], cell = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else quoted = false; } else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(cell); cell = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  const clean = rows.filter(r => r.some(c => c.trim()));
  if (!clean.length) return [];
  const head = clean[0].map(h => ALIAS[h.trim().toLowerCase().replace(/[^a-z]+/g, '_').replace(/^_|_$/g, '')] || null);
  return clean.slice(1).map(r => {
    const o: Record<string, string> = {};
    head.forEach((k, i) => { if (k) o[k] = (r[i] || '').trim(); });
    return o;
  });
}
