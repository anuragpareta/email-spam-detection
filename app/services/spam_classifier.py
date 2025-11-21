from transformers import pipeline

class SpamClassifier:
    def __init__(self):
        self.classifier = pipeline(
            "text2text-generation",
            model="google/flan-t5-base",
            max_length=8,
            device=-1,  # CPU device
            batch_size=1
        )

    def classify_email(self, sender, subject, body):
        body_snip = (body or "")[:256]
        prompt = (
            f"Does the following email seem like spam?\n"
            f"Sender: {sender}\n"
            f"Subject: {subject}\n"
            f"Body: {body_snip}\n"
            f"Answer with one word: spam or not-spam."
        )
        output = self.classifier(prompt)[0]['generated_text'].strip().lower()
        return "spam" if "spam" in output else "not-spam"
