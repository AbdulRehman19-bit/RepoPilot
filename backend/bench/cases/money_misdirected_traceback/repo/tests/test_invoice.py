from billing.invoice import Invoice


def test_invoice_total():
    invoice = Invoice()
    invoice.add_line("work", "100")
    invoice.add_line("parts", "$20.25")
    assert invoice.total() == 120.25
