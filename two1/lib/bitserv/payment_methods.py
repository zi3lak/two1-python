"""Allowed payment methods."""
import json
import requests

from two1.lib.bitcoin.txn import Transaction
from two1.lib.blockchain.twentyone_provider import TwentyOneProvider
from two1.commands.config import TWO1_HOST
from two1.commands.config import TWO1_CONFIG_FILE
from .models import OnChainSQLite3
from .payment_server import PaymentServer


class PaymentError(Exception):
    pass


class InsufficientPaymentError(PaymentError):
    pass


class InvalidPaymentParameterError(PaymentError):
    pass


class DuplicatePaymentError(PaymentError):
    pass


class TransactionBroadcastError(PaymentError):
    pass


class ServerError(Exception):
    pass


class PaymentBase:

    """Base class for payment methods."""

    def should_redeem(self, request_headers):
        """Method for checking if we should use a derived payment method."""
        return all(h in request_headers.keys() for h in self.payment_headers)

    @property
    def payment_headers(self):
        """Derived list of headers to use for payment processing.

        Returns:
            (list): List of required headers that a client should present
                in order to redeem a payment of this method.
                Example: ['Bitcoin-Transaction']
        """
        raise NotImplementedError()

    def get_402_headers(self, price, **kwargs):
        """Derived dict of headers to return in the initial 402 response.

        Args:
            price: Endpoint price in satoshis
            (**kwargs):
        Returns:
            (dict): Dict of headers that the server uses to inform the client
                how to remit payment for the resource.
                Example: {'address': '1MDxJYsp4q4P46RiigaGzrdyi3dsNWCTaR',
                          'price': 500}
        """
        raise NotImplementedError()

    def redeem_payment(self, price, request_headers, payment_headers):
        """Derived method for processing and validating payment.

        Args:
            request_headers (dict): Headers sent by client with their request.
            payment_headers (dict): Required headers to verify the client's
                request against.
        Returns:
            (boolean): Whether or not the payment is valid.
        Raises:
            InsufficientPaymentError: Payment received does not match the
                resource's price.
            InvalidPaymentParameterError: Payment was made to an address other
                than the merchant's provided address.
            DuplicatePaymentError: Payment has already been used to pay for a
                resource from the merchant.
        """
        raise NotImplementedError()


###############################################################################


class OnChain(PaymentBase):

    """Making a payment on the bitcoin blockchain."""

    http_payment_data = 'Bitcoin-Transaction'
    http_402_price = 'Price'
    http_402_address = 'Bitcoin-Address'

    def __init__(self, wallet, db=None):
        """Initialize payment handling for on-chain payments."""
        self.db = db or OnChainSQLite3()
        self.address = wallet.get_payout_address()
        self.provider = TwentyOneProvider(TWO1_HOST)

    @property
    def payment_headers(self):
        """List of headers to use for payment processing."""
        return [OnChain.http_payment_data]

    def get_402_headers(self, price, **kwargs):
        """Dict of headers to return in the initial 402 response."""
        return {OnChain.http_402_price: price,
                OnChain.http_402_address: kwargs.get('address', self.address)}

    def redeem_payment(self, price, request_headers, **kwargs):
        """Validate the transaction and broadcast it to the blockchain."""
        raw_tx = request_headers[OnChain.http_payment_data]
        print('Receieved transaction: {}'.format(raw_tx))
        try:
            payment_tx = Transaction.from_hex(raw_tx)
        except:
            raise InvalidPaymentParameterError('Error: Invalid tx hex.')

        # Find the output with the merchant's address
        payment_index = payment_tx.output_index_for_address(kwargs.get('address', self.address))
        if payment_index is None:
            raise InvalidPaymentParameterError('Error: Not paid to merchant.')

        # Verify that the payment is made for the correct amount
        if payment_tx.outputs[payment_index].value != price:
            raise InsufficientPaymentError('Error: Incorrect payment amount.')

        # Store and verify that we haven't seen this payment before
        txid, new = self.db.get_or_create(str(payment_tx.hash), price)
        if not new:
            raise DuplicatePaymentError('Error: Payment already used.')

        # Broadcast payment
        try:
            txid = self.provider.broadcast_transaction(raw_tx)
            print('Broadcasted: ' + txid)
        except Exception as e:
            raise TransactionBroadcastError(str(e))

        return True


class PaymentChannel(PaymentBase):

    """Making a payment within a payment channel."""

    http_payment_token = 'Bitcoin-Micropayment-Token'
    http_402_price = 'Price'
    http_402_micro_server = 'Bitcoin-Micropayment-Server'

    def __init__(self, server, endpoint_path):
        """Initialize payment handling for on-chain payments."""
        self.server = server
        self.endpoint_path = endpoint_path

    @property
    def payment_headers(self):
        """List of headers to use for payment processing."""
        return [PaymentChannel.http_payment_token]

    def get_402_headers(self, price, **kwargs):
        """Dict of headers to return in the initial 402 response."""
        return {PaymentChannel.http_402_price: price,
                PaymentChannel.http_402_micro_server: kwargs['server_url'] + self.endpoint_path}

    def redeem_payment(self, price, request_headers, **kwargs):
        """Validate the micropayment and redeem it."""
        txid = request_headers[PaymentChannel.http_payment_token]
        validated = False
        try:
            # Redeem the transaction in its payment channel
            paid_amount = int(self.server.redeem(txid))
            # Verify the amount of the payment against the resource price
            if paid_amount == int(price):
                validated = True
        except Exception as e:
            raise e
        return validated

class BitTransfer(PaymentBase):

    """Making a payment via 21 BitTransfer protocol."""

    http_payment_data = 'Bitcoin-Transfer'
    http_402_price = 'Price'
    http_402_address = 'Bitcoin-Address'
    http_authorization = 'Authorization'
    http_402_username = 'Username'

    verification_url = TWO1_HOST + '/pool/account/{}/bittransfer/'
    account_file = TWO1_CONFIG_FILE
    
    def __init__(self, wallet, verification_url=None, seller_account=None):
        """Initialize payment handling for on-chain payments."""
        self.address = wallet.get_payout_address()
        self.verification_url = verification_url or BitTransfer.verification_url
        acct = seller_account or BitTransfer.account_file
        
        with open(acct, 'r') as f:
            account = json.loads(f.read())
        seller = account['username']
        self.seller_username = seller

    @property
    def payment_headers(self):
        """List of headers to use for payment processing."""
        return [BitTransfer.http_payment_data, BitTransfer.http_authorization]

    def get_402_headers(self, price, **kwargs):
        """Dict of headers to return in the initial 402 response."""
        return {BitTransfer.http_402_price: price,
                BitTransfer.http_402_address: self.address,
                BitTransfer.http_402_username: self.seller_username}

    def redeem_payment(self, price, request_headers, **kwargs):
        """Verify that the BitTransfer is valid.

            (1) Check that amount sent in transfer
                is correct
            (2) Authenticate & verify via 3rd party
                (21.co) server.
        """
        # extract bittransfer & sig from headers
        bittransfer = request_headers[BitTransfer.http_payment_data]
        signature = request_headers[BitTransfer.http_authorization]

        # check amount in transfer
        resource_price = price
        if not json.loads(bittransfer)["amount"] == resource_price:
            raise InsufficientPaymentError('Error: Incorrect payment amount.')
        
        # now verify with 21.co server that transfer is valid
        try:
            verification_response = requests.post(
                self.verification_url.format(
                    self.seller_username
                ),
                data=json.dumps({
                    "bittransfer": bittransfer,
                    "signature": signature
                }),
                headers={'content-type': 'application/json'}
            )
            if verification_response.ok:
                return True
        except requests.ConnectionError:
            print("Client failed to connect to server.")

        # handle verification server bad response
        if "message" in verification_response.text:
            raise InvalidPaymentParameterError(
                verification_response.json()["message"]
            )
        else:
            raise ServerError