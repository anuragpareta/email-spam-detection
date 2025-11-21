def test_classify_spam():
    classifier = SpamClassifier()
    label = classifier.classify_email("sender@example.com", "Win prize", "Free money offer...")
    assert label in ["spam", "not spam"]
