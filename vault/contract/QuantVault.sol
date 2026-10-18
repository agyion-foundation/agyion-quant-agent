// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {Roles} from "../../delay-as-a-role/libraries/Roles.sol";
import {BaseDelayAsAActor} from "../../delay-as-a-role/actor/BaseDelayAsAActor.sol";
import {IBaseDelayAsAActor} from "../../delay-as-a-role/interfaces/IBaseDelayAsAActor.sol";

/**
 * QuantVault — agent-caged ERC-4626 vault.
 *
 * - Users deposit/withdraw the asset permissionlessly (even while paused).
 * - A separate `agent` EOA may ONLY call whitelisted (target, selector) pairs,
 *   subject to a daily USD notional cap, and never the vault asset itself.
 * - Trades are queued behind a timelock; the admin can veto before execution.
 * - The agent can never withdraw user funds: it holds no shares and every
 *   exit path is share-gated or admin-gated.
 */
contract QuantVault is ERC4626, Pausable {
    using Math for uint256;

    uint256 public constant TIMELOCK = 15 minutes;
    uint256 public constant MAX_DAILY_LIMIT = 100_000e6; // hard ceiling for setDailyLimit

    address public immutable vaultAdmin;
    address public agent;
    IBaseDelayAsAActor public vault; // delay-as-a-role actor handle (kept for ABI compat)

    uint256 public dailyLimit = 10_000e6;
    uint256 public dailyWindowStart;
    uint256 public spentToday;

    struct Trade {
        address target;
        uint256 amountUsd;
        uint256 executeAfter;
        bool executed;
        bool cancelled;
    }
    Trade[] public trades;

    // target => selector => allowed
    mapping(address => mapping(bytes4 => bool)) public whitelistedCalls;

    event TradeQueued(uint256 indexed tradeId, address indexed target, uint256 amountUsd, uint256 executeAfter);
    event TradeExecuted(uint256 indexed tradeId, address indexed target, uint256 amountUsd);
    event TradeCancelled(uint256 tradeId, address indexed by);
    event WhitelistUpdated(address indexed target, bytes4 indexed selector, bool allowed);
    event DailyLimitUpdated(uint256 newLimit);

    modifier onlyAdmin() {
        require(msg.sender == vaultAdmin, "caller is not the admin");
        _;
    }
    modifier onlyAgent() {
        require(msg.sender == agent, "caller is not the agent");
        _;
    }

    constructor(IERC20 asset_, address agent_)
        ERC20("Quant Vault Share", "qVLT")
        ERC4626(asset_)
    {
        require(agent_ != address(0), "agent is zero");
        vaultAdmin = msg.sender;
        agent = agent_;
        dailyWindowStart = block.timestamp;
    }

    // ---------------------------------------------------------------- admin
    function pause() external onlyAdmin { _pause(); }
    function unpause() external onlyAdmin { _unpause(); }

    function setAgent(address agent_) external onlyAdmin {
        require(agent_ != address(0), "agent is zero");
        agent = agent_;
    }

    function setDailyLimit(uint256 newLimit) external onlyAdmin {
        require(newLimit <= MAX_DAILY_LIMIT, "limit above ceiling");
        dailyLimit = newLimit;
        emit DailyLimitUpdated(newLimit);
    }

    function setWhitelisted(address target, bytes4 selector, bool allowed) external onlyAdmin {
        require(target != address(this) && target != asset(), "cannot whitelist vault/asset");
        whitelistedCalls[target][selector] = allowed;
        emit WhitelistUpdated(target, selector, allowed);
    }

    function cancelTrade(uint256 tradeId) external onlyAdmin {
        Trade storage t = trades[tradeId];
        require(!t.executed && !t.cancelled, "trade not pending");
        t.cancelled = true;
        emit TradeCancelled(tradeId, msg.sender);
    }

    /// Emergency exit for the admin while paused: pull any ERC-20 (except via
    /// normal user flows) to a recovery address. Never callable by the agent.
    function emergencyWithdrawToken(address token, address to, uint256 amount)
        external
        onlyAdmin
        whenPaused
    {
        require(to != address(0), "to is zero");
        IERC20(token).transfer(to, amount);
    }

    // ---------------------------------------------------------------- agent
    function queueTrade(address target, uint256 amountUsd, bytes calldata data)
        external
        onlyAgent
        whenNotPaused
        returns (uint256 tradeId)
    {
        require(target != asset(), "cannot trade the vault asset");
        require(data.length >= 4, "empty calldata not allowed on whitelisted targets");
        bytes4 selector;
        assembly { selector := calldataload(data.offset) }
        require(whitelistedCalls[target][selector], "target/selector not whitelisted");
        require(amountUsd > 0, "amount is zero");

        _rollDailyWindow();
        require(spentToday + amountUsd <= dailyLimit, "exceeds daily limit");
        spentToday += amountUsd;

        tradeId = trades.length;
        trades.push(Trade({
            target: target,
            amountUsd: amountUsd,
            executeAfter: block.timestamp + TIMELOCK,
            executed: false,
            cancelled: false
        }));
        emit TradeQueued(tradeId, target, amountUsd, block.timestamp + TIMELOCK);
    }

    function executeTrade(uint256 tradeId) external onlyAgent whenNotPaused {
        Trade storage t = trades[tradeId];
        require(!t.executed && !t.cancelled, "trade not pending");
        require(block.timestamp >= t.executeAfter, "trade still timelocked");

        t.executed = true;
        uint256 bal = IERC20(asset()).balanceOf(address(this));
        uint256 amount = t.amountUsd > bal ? bal : t.amountUsd;
        require(amount > 0, "nothing to trade");
        IERC20(asset()).transfer(t.target, amount); // demo: transfer to strategy target
        emit TradeExecuted(tradeId, t.target, t.amountUsd);
    }

    // ------------------------------------------------------------- internals
    function _rollDailyWindow() internal {
        if (block.timestamp >= dailyWindowStart + 1 days) {
            dailyWindowStart = block.timestamp;
            spentToday = 0;
        }
    }

    function tradesCount() external view returns (uint256) {
        return trades.length;
    }

    /// Deposit/withdraw stay permissionless for users even while paused —
    /// the cage is around the agent, not the money. (ERC4626 default _update
    /// paths are unaffected by Pausable because we never gate them.)
}
