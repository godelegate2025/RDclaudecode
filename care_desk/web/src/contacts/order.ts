import type { Id } from '../types';

// The order the Contacts list showed when a contact was opened, so the contact's page can step
// to the previous and next contact as they were listed. Lost on a reload, like the page's place.
let order: Id[] = [];
export const rememberOrder = (ids: Id[]) => { order = ids; };
export const listedOrder = () => order;
