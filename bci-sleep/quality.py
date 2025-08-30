# quality.py
def is_all_zero(vec):
    return all((v == 0 or v is None) for v in vec)

def safe_get(d, key, default=0.0):
    v = d.get(key, default)
    return default if v is None else v
