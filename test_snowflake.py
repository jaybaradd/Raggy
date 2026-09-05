import torch
from sentence_transformers import SentenceTransformer
from sentence_transformers.util import cos_sim

print("Loading snowflake-arctic-embed-m-v2.0-cpu model on CPU...")
device = torch.device("cpu")
model = SentenceTransformer(
    "cnmoro/snowflake-arctic-embed-m-v2.0-cpu",
    device=device,
    trust_remote_code=True,
)
model.truncate_dim = 256

text1 = "how many internships did he do?"
text2 = "Data Science Intern at Collegestreet.tech (Mumbai). Held from February 2025 to June 2025."

print(f"\nEncoding Text 1: '{text1}'")
print(f"Encoding Text 2: '{text2}'")

emb1 = model.encode(text1, convert_to_tensor=True)
emb2 = model.encode(text2, convert_to_tensor=True)

similarity = cos_sim(emb1, emb2).item()

print(f"\nEmbedding Dimension: {emb1.shape[-1]}")
print(f"Cosine Similarity Score: {similarity:.4f}")
print("\n✅ Model loaded with 256 dimensions and similarity calculated successfully!")