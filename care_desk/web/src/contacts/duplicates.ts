import type { Id, Lead } from '../types';

// Names that say nothing about who it is (as NAMELESS in care_desk/desk.py).
const NAMELESS = new Set(['', 'unknown', 'anonymous', 'guest', 'customer', 'user', 'facebook user',
  'instagram user', 'tiktok user', 'linkedin member']);

/** A name as the desk compares it: case, spacing, punctuation and accents do not matter. */
export function nameKey(name: string): string {
  const plain = (name || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();
  const key = (plain.match(/[a-z0-9]+/g) || []).join(' ');
  return NAMELESS.has(key) ? '' : key;
}

/* Contacts that may be one person saved twice: the same name, email or GHL contact. The desk
   only files a message under a contact automatically when nothing could make it the wrong
   person, so these are left for someone to merge, or to mark as two different people. */
export function possibleDuplicates(leads: Lead[]): Map<Id, Lead[]> {
  const groups = new Map<string, Lead[]>();
  const group = (key: string, l: Lead) => { if (!groups.has(key)) groups.set(key, []); groups.get(key)!.push(l); };
  for (const l of leads) {
    const name = nameKey(l.name);
    if (name) group('name:' + name, l);
    if (l.email) group('email:' + l.email.toLowerCase(), l);
    if (l.contact_id) group('ghl:' + l.contact_id, l);
  }
  const found = new Map<Id, Map<Id, Lead>>();
  const apart = (a: Lead, b: Lead) => !!a.distinct?.includes(b.id) || !!b.distinct?.includes(a.id);
  for (const same of groups.values()) {
    for (const a of same) for (const b of same) {
      if (a.id === b.id || apart(a, b)) continue;
      if (!found.has(a.id)) found.set(a.id, new Map());
      found.get(a.id)!.set(b.id, b);
    }
  }
  return new Map([...found].map(([id, others]) => [id, [...others.values()]]));
}
