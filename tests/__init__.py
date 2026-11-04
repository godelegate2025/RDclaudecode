# No test talks to the real Firestore: CI runs where Google credentials may be
# reachable, so both stores start in memory and tests swap in their own.
from service import email_login, history, profiles, search_history, team

team.use_store(team.MemoryStore())
history.use_store(history.MemoryStore())
search_history.use_store(search_history.memory_store())
profiles.use_store(profiles.MemoryStore())
email_login.use_store(email_login.MemoryStore())
