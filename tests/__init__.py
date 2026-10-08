# No test talks to the real Firestore: CI runs where Google credentials may be
# reachable, so both stores start in memory and tests swap in their own.
from service import history, team

team.use_store(team.MemoryStore())
history.use_store(history.MemoryStore())
