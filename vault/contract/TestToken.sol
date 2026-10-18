// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title TestToken — Fuji testnet için minimal ERC-20 (vault testleri + demo deposit)
/// @notice SADECE testnet içindir. Herkes mint edebilir (faucet mantığı).
contract TestToken {
    string public name;
    string public symbol;
    uint8 public constant decimals = 6; // USDC-benzeri

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor(string memory name_, string memory symbol_) {
        name = name_;
        symbol = symbol_;
    }

    /// @notice Testnet faucet-mint: herkes kendine token basabilir.
    function mint(address to, uint256 amount) external {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _move(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "TestToken: allowance");
        if (allowed != type(uint256).max) {
            allowance[from][msg.sender] = allowed - amount;
        }
        _move(from, to, amount);
        return true;
    }

    function _move(address from, address to, uint256 amount) internal {
        require(balanceOf[from] >= amount, "TestToken: balance");
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}

/// @title MockRouter — whitelist/selector testleri için sahte DEX router'ı.
/// @notice swap() çağrıldığında sadece event basar; gerçek token hareketi yapmaz.
contract MockRouter {
    event Swapped(address indexed caller, uint256 amountIn, bytes data);

    /// @dev selector: bytes4(keccak256("swap(uint256)"))
    function swap(uint256 amountIn) external returns (bool) {
        emit Swapped(msg.sender, amountIn, msg.data);
        return true;
    }

    /// @dev Her zaman revert eden fonksiyon (başarısız-trade testi için).
    function swapReverting(uint256) external pure returns (bool) {
        revert("MockRouter: forced revert");
    }
}
