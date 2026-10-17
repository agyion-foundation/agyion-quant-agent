// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title QuantVault — Agyion Quant Agent non-custodial vault (Avalanche Fuji)
/// @notice ERC-4626-benzeri basit vault. Kullanıcı (owner/guardian) her zaman
///         deposit/withdraw edebilir. Ayrı bir "agent" adresi SADECE whitelist'li
///         hedef kontratların whitelist'li fonksiyon selector'larını, timelock
///         gecikmesi ve günlük notional limiti dahilinde çağırabilir.
///         Agent asla withdraw/transfer edemez.
///
/// OpenZeppelin bağımlılığı yoktur (sandbox'ta npm yok); ReentrancyGuard ve
/// Ownable kalıpları OZ ile aynı semantikte lokal olarak uygulanmıştır.
/// Remix/Hardhat ile derlenecekse bu dosya tek başına yeterlidir.

interface IERC20 {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function decimals() external view returns (uint8);
}

/// @dev OpenZeppelin ReentrancyGuard ile aynı semantik (lokal stub).
abstract contract ReentrancyGuard {
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status;

    constructor() {
        _status = _NOT_ENTERED;
    }

    modifier nonReentrant() {
        require(_status != _ENTERED, "ReentrancyGuard: reentrant call");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }
}

/// @dev OpenZeppelin Ownable ile aynı semantik (lokal stub).
abstract contract Ownable {
    address private _owner;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    constructor() {
        _owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    modifier onlyOwner() {
        require(msg.sender == _owner, "Ownable: caller is not the owner");
        _;
    }

    function owner() public view returns (address) {
        return _owner;
    }

    function transferOwnership(address newOwner) public onlyOwner {
        require(newOwner != address(0), "Ownable: zero address");
        emit OwnershipTransferred(_owner, newOwner);
        _owner = newOwner;
    }
}

contract QuantVault is ReentrancyGuard, Ownable {
    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------

    IERC20 public immutable asset;          // vault'un baz varlığı (ör. test USDC)
    address public agent;                   // trade önerebilen/çağırabilen agent adresi

    uint256 public totalShares;             // toplam vault payı
    mapping(address => uint256) public sharesOf;

    bool public paused;                     // guardian pause — sadece agent işlemlerini durdurur

    uint256 public timelockDelay;           // agent trade'leri için gecikme (saniye)
    uint256 public maxDailyNotional;        // agent günlük işlem üst sınırı (asset birimi)
    uint256 public spentNotionalDay;        // hangi güne ait sayaç (block.timestamp / 1 days)
    uint256 public spentNotional;           // o gün execute edilmiş toplam notional
    uint256 public pendingNotional;         // kuyrukta bekleyen (veto/execute edilmemiş) notional

    // whitelist: target kontrat → selector → izinli mi
    mapping(address => mapping(bytes4 => bool)) public allowedCalls;
    // target hiç whitelist'li mi (selector temizlenince de target'ın izni kontrolü için)
    mapping(address => bool) public whitelistedTargets;

    struct QueuedTrade {
        address target;
        bytes data;
        uint256 notional;                   // bu işlemin tahmini notional'i (asset birimi)
        uint256 eta;                        // execute edilebilecek en erken zaman
        bool executed;
        bool vetoed;
    }

    QueuedTrade[] public tradeQueue;

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------

    event Deposited(address indexed user, uint256 assets, uint256 shares);
    event Withdrawn(address indexed user, uint256 assets, uint256 shares);
    event TradeQueued(uint256 indexed id, address indexed target, bytes4 selector, uint256 notional, uint256 eta);
    event TradeExecuted(uint256 indexed id, address indexed target, bytes4 selector, uint256 notional);
    event TradeVetoed(uint256 indexed id, address indexed guardian);
    event SignalRecorded(int256 indexed composite, string signal, uint256 targetExposureBps);
    event AgentUpdated(address indexed oldAgent, address indexed newAgent);
    event CallWhitelistUpdated(address indexed target, bytes4 indexed selector, bool allowed);
    event Paused(address indexed guardian);
    event Unpaused(address indexed guardian);
    event TimelockUpdated(uint256 oldDelay, uint256 newDelay);
    event MaxDailyNotionalUpdated(uint256 oldLimit, uint256 newLimit);

    // ------------------------------------------------------------------
    // Modifiers
    // ------------------------------------------------------------------

    modifier onlyAgent() {
        require(msg.sender == agent, "QuantVault: caller is not the agent");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "QuantVault: paused");
        _;
    }

    // ------------------------------------------------------------------
    // Constructor
    // ------------------------------------------------------------------

    constructor(
        address asset_,
        address agent_,
        uint256 timelockDelay_,
        uint256 maxDailyNotional_
    ) {
        require(asset_ != address(0), "QuantVault: zero asset");
        asset = IERC20(asset_);
        agent = agent_;
        timelockDelay = timelockDelay_;
        maxDailyNotional = maxDailyNotional_;
        spentNotionalDay = block.timestamp / 1 days;
    }

    // ------------------------------------------------------------------
    // Kullanıcı fonksiyonları — ASLA pause edilemez, izin gerektirmez
    // ------------------------------------------------------------------

    /// @notice Vault toplam varlığı (asset cinsinden bakiye).
    function totalAssets() public view returns (uint256) {
        return asset.balanceOf(address(this));
    }

    /// @notice Asset yatır, karşılığında share mint'le. İzin gerekmez, pause'dan etkilenmez.
    function deposit(uint256 assets) external nonReentrant returns (uint256 shares) {
        require(assets > 0, "QuantVault: zero deposit");
        uint256 supply = totalShares;
        if (supply == 0) {
            shares = assets; // ilk yatırma 1:1
        } else {
            shares = (assets * supply) / totalAssets();
            require(shares > 0, "QuantVault: shares too small");
        }
        require(asset.transferFrom(msg.sender, address(this), assets), "QuantVault: transfer failed");
        sharesOf[msg.sender] += shares;
        totalShares = supply + shares;
        emit Deposited(msg.sender, assets, shares);
    }

    /// @notice Share yak, orantılı asset çek. HER ZAMAN çalışır (pause edilemez).
    ///         Agent bu fonksiyonu çağırabilir ama sadece kendi share'i kadar çeker;
    ///         agent'a share mint'lenmediği için pratikte fonlara erişimi yoktur.
    function withdraw(uint256 shares) external nonReentrant returns (uint256 assetsOut) {
        require(shares > 0, "QuantVault: zero shares");
        require(sharesOf[msg.sender] >= shares, "QuantVault: insufficient shares");
        assetsOut = (shares * totalAssets()) / totalShares;
        require(assetsOut > 0, "QuantVault: zero assets");
        sharesOf[msg.sender] -= shares;
        totalShares -= shares;
        require(asset.transfer(msg.sender, assetsOut), "QuantVault: transfer failed");
        emit Withdrawn(msg.sender, assetsOut, shares);
    }

    // ------------------------------------------------------------------
    // Agent fonksiyonları — kısıtlı, gecikmeli, sınırlı
    // ------------------------------------------------------------------

    /// @notice Agent sinyali zincire kaydeder (kanıt/defter).
    /// @param composite  endeks bileşik skoru, 1e18 ölçekli, [-1e18, +1e18]
    /// @param signal     "BUY" / "NEUTRAL" / "SELL"
    /// @param targetExposureBps hedef maruziyet, baz puanı (0..10000)
    function recordSignal(int256 composite, string calldata signal, uint256 targetExposureBps)
        external
        onlyAgent
        whenNotPaused
    {
        require(composite >= -1e18 && composite <= 1e18, "QuantVault: composite out of range");
        require(targetExposureBps <= 10000, "QuantVault: exposure > 100%");
        emit SignalRecorded(composite, signal, targetExposureBps);
    }

    /// @notice Agent trade'i kuyruğa ekler. Timelock sonrası execute edilebilir.
    function queueTrade(address target, bytes calldata data, uint256 notional)
        external
        onlyAgent
        whenNotPaused
        returns (uint256 id)
    {
        require(target != address(0), "QuantVault: zero target");
        require(target != address(asset), "QuantVault: cannot call asset directly");
        require(whitelistedTargets[target], "QuantVault: target not whitelisted");
        require(data.length >= 4, "QuantVault: data too short");
        bytes4 selector = bytes4(data);
        require(allowedCalls[target][selector], "QuantVault: selector not whitelisted");

        _previewDailyNotional(notional); // spent + pending + notional <= limit
        pendingNotional += notional;

        id = tradeQueue.length;
        uint256 eta = block.timestamp + timelockDelay;
        tradeQueue.push(QueuedTrade({
            target: target,
            data: data,
            notional: notional,
            eta: eta,
            executed: false,
            vetoed: false
        }));
        emit TradeQueued(id, target, selector, notional, eta);
    }

    /// @notice Timelock geçmiş, veto edilmemiş trade'i çalıştırır.
    function executeTrade(uint256 id)
        external
        onlyAgent
        whenNotPaused
        nonReentrant
    {
        require(id < tradeQueue.length, "QuantVault: bad id");
        QueuedTrade storage t = tradeQueue[id];
        require(!t.executed, "QuantVault: already executed");
        require(!t.vetoed, "QuantVault: vetoed");
        require(block.timestamp >= t.eta, "QuantVault: timelock not elapsed");
        require(whitelistedTargets[t.target], "QuantVault: target not whitelisted");
        require(t.data.length >= 4, "QuantVault: data too short");
        bytes4 selector = bytes4(t.data);
        require(allowedCalls[t.target][selector], "QuantVault: selector not whitelisted");

        pendingNotional -= t.notional;
        _spendDailyNotional(t.notional); // günlük sayaç execute anında işlenir

        t.executed = true;
        (bool ok, bytes memory ret) = t.target.call(t.data);
        require(ok, _getRevertMsg(ret));
        emit TradeExecuted(id, t.target, selector, t.notional);
    }

    // ------------------------------------------------------------------
    // Guardian (owner) fonksiyonları
    // ------------------------------------------------------------------

    function pause() external onlyOwner {
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyOwner {
        paused = false;
        emit Unpaused(msg.sender);
    }

    /// @notice Kuyruktaki trade'i veto et (henüz execute edilmemişse).
    function vetoTrade(uint256 id) external onlyOwner {
        require(id < tradeQueue.length, "QuantVault: bad id");
        QueuedTrade storage t = tradeQueue[id];
        require(!t.executed, "QuantVault: already executed");
        require(!t.vetoed, "QuantVault: already vetoed");
        t.vetoed = true;
        pendingNotional -= t.notional; // veto edilen notional limiti serbest bırakır
        emit TradeVetoed(id, msg.sender);
    }

    function setAgent(address newAgent) external onlyOwner {
        emit AgentUpdated(agent, newAgent);
        agent = newAgent;
    }

    /// @notice Hedef kontrat + selector whitelist'i güncelle.
    function setAllowedCall(address target, bytes4 selector, bool allowed) external onlyOwner {
        require(target != address(0), "QuantVault: zero target");
        require(target != address(asset), "QuantVault: cannot whitelist asset");
        allowedCalls[target][selector] = allowed;
        whitelistedTargets[target] = allowed || whitelistedTargets[target] && _hasAnySelector(target);
        if (allowed) {
            whitelistedTargets[target] = true;
        }
        emit CallWhitelistUpdated(target, selector, allowed);
    }

    /// @notice Target'ın whitelist'ini tamamen kaldır.
    function removeTarget(address target) external onlyOwner {
        whitelistedTargets[target] = false;
        emit CallWhitelistUpdated(target, bytes4(0), false);
    }

    function setTimelockDelay(uint256 newDelay) external onlyOwner {
        emit TimelockUpdated(timelockDelay, newDelay);
        timelockDelay = newDelay;
    }

    function setMaxDailyNotional(uint256 newLimit) external onlyOwner {
        emit MaxDailyNotionalUpdated(maxDailyNotional, newLimit);
        maxDailyNotional = newLimit;
    }

    // ------------------------------------------------------------------
    // View helpers
    // ------------------------------------------------------------------

    function queueLength() external view returns (uint256) {
        return tradeQueue.length;
    }

    function remainingDailyNotional() external view returns (uint256) {
        if ((block.timestamp / 1 days) != spentNotionalDay) {
            return maxDailyNotional;
        }
        if (spentNotional >= maxDailyNotional) return 0;
        return maxDailyNotional - spentNotional;
    }

    // ------------------------------------------------------------------
    // Internal
    // ------------------------------------------------------------------

    /// @dev Kuyruğa ekleme anında kontrol: bugün harcanan + beklemede olan + yeni <= limit.
    function _previewDailyNotional(uint256 notional) internal view {
        uint256 spent = ((block.timestamp / 1 days) != spentNotionalDay) ? 0 : spentNotional;
        require(spent + pendingNotional + notional <= maxDailyNotional, "QuantVault: daily limit exceeded");
    }

    /// @dev Execute anında notional'i günlük sayaca işler.
    function _spendDailyNotional(uint256 notional) internal {
        uint256 today = block.timestamp / 1 days;
        if (today != spentNotionalDay) {
            spentNotionalDay = today;
            spentNotional = 0;
        }
        require(spentNotional + notional <= maxDailyNotional, "QuantVault: daily limit exceeded");
        spentNotional += notional;
    }

    /// @dev Selector'lar mapping'te enumerate edilemediği için removeTarget
    ///      dışındaki "tam kaldırma" senaryosunda hedefin başka izinli selector'ü
    ///      olup olmadığını bilemeyiz; güvenli varsayılan: target'ı sadece
    ///      açıkça removeTarget ile kaldırırız. Bu helper gelecekteki zincir-içi
    ///      enumerate için yer tutucudur; şu an her zaman false döner.
    function _hasAnySelector(address) internal pure returns (bool) {
        return false;
    }

    function _getRevertMsg(bytes memory ret) internal pure returns (string memory) {
        if (ret.length < 68) return "QuantVault: low-level call failed";
        assembly {
            ret := add(ret, 4)
        }
        return abi.decode(ret, (string));
    }
}
