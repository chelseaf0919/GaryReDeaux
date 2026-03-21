import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
sb = create_client(os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY"))

rows = sb.table("personality_samples")\
    .select("excerpt")\
    .ilike("excerpt", "%Sendient%")\
    .limit(3)\
    .execute()

print(f"Found {len(rows.data)} Sendient samples in Supabase")
for r in rows.data:
    print(r["excerpt"][:200])
    print("---")