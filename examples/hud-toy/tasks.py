from env import count_letter

WORDS = ["strawberry", "banana", "mississippi", "bookkeeper", "committee", "parallel", "assessment", "occurrence",
         "possession", "referee", "tattoo", "balloon", "coffee", "letter", "success", "address"]

tasks = [count_letter(word=w, letter=w[len(w) // 2]) for w in WORDS]
