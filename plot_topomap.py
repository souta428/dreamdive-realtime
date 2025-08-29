import numpy as np
import scipy.interpolate
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.colors import Normalize

from config import SHOW_CHANNELS

def plot_topomap(data, ax, fig, draw_cbar=True, highlight_frontal_lobe=False):
    """
    脳波の各チャンネルのデータを基に，頭部のトポグラフマップ（Topomap）を描画する関数．

    Args:
        data (list or array-like): トポグラフにプロットするデータ．`SHOW_CHANNELS` に対応した順番で，
                                   各チャンネルの値（例：相対パワーまたはzスコアなど）を含む．
        ax (matplotlib.axes.Axes): 描画を行うMatplotlibの軸オブジェクト．
        fig (matplotlib.figure.Figure): カラーバーを表示するためのMatplotlibの図オブジェクト．
        draw_cbar (bool, optional): カラーバーを表示するかどうか．デフォルトは `True`．
        highlight_frontal_lobe (bool, optional): 前頭葉電極 (`Fp1`, `Fp2`, `F3`, `F4`) を強調するかどうか．デフォルトは `False`．

    Returns:
        matplotlib.axes.Axes: プロットされたトポグラフマップを含む軸オブジェクト．
    """

    # -------------------------
    # ここがトポマップ全体の設定
    # -------------------------
    # グリッド解像度
    N = 300  
    # 頭の中心座標（2次元）
    xy_center = [2, 2]  
    # 頭の半径（描画時は x=[0,4], y=[0,4] あたりが頭部）
    radius = 2

    # ---------------------------------------------------------
    # 10/20法を平面に投影した場合の、おおよその座標設定例
    # （中心を (2,2) とし、半径=2 の円内に配置）
    # ---------------------------------------------------------
    # 下記の通り “真上から見た状態” でおおよその位置関係をとっています。
    # - Fp1, Fp2 は前頭極（前方）
    # - O1, O2 は後頭（後方）
    # - F3, F4, C3, C4, P3, P4 はそれぞれ中央より少し前/後
    # - F7, F8, T3, T4, T5, T6 は側頭付近
    #
    # ※ 実際の臨床では頭の個人差で若干位置が変わるため、あくまでも参考値です
    # ※ 縦軸(y)が大きいほど頭頂・前頭側、yが小さいほど後頭側、x=2 が正中線付近
    ch_pos = [
        [1.3, 3.7],  # Fp1
        [2.7, 3.7],  # Fp2
        [1.25, 3.0], # F3
        [2.75, 3.0], # F4
        [1.0, 2.0],  # C3
        [3.0, 2.0],  # C4
        [1.25, 1.0], # P3
        [2.75, 1.0], # P4
        [1.4, 0.3],  # O1
        [2.6, 0.3],  # O2
        [0.4, 2.9],  # F7
        [3.6, 2.9],  # F8
        [0.2, 2.0],  # T3
        [3.8, 2.0],  # T4
        [0.5, 1.0],  # T5
        [3.5, 1.0],  # T6
    ]

    # x, y に分解
    x, y = zip(*ch_pos)

    # 補間用のグリッド作成
    xi = np.linspace(-2, 6, N)
    yi = np.linspace(-2, 6, N)

    # スプライン補間
    zi = scipy.interpolate.griddata((x, y), data, (xi[None, :], yi[:, None]), method='cubic')

    # 頭部（円形）より外側を NaN に置き換えてマスクする
    dr = xi[1] - xi[0]  # グリッドの刻み幅
    for i in range(N):
        for j in range(N):
            r = np.sqrt((xi[i] - xy_center[0])**2 + (yi[j] - xy_center[1])**2)
            if (r - dr/2) > radius:
                zi[j, i] = np.nan

    # トポマップの塗りつぶし等高線
    dist = ax.contourf(xi, yi, zi, 60, cmap=plt.get_cmap('bwr'),
                       zorder=1, norm=Normalize(vmin=-2.5, vmax=2.5))
    # 等高線（境界線）
    ax.contour(xi, yi, zi, 15, linewidths=0.5, colors="grey", zorder=2)

    # カラーバーを表示
    if draw_cbar:
        cbar = fig.colorbar(dist, ax=ax, format='%.1f')
        cbar.ax.tick_params(labelsize=8)

    # 前頭葉電極を黒色で強調表示
    if highlight_frontal_lobe:
        # `SHOW_CHANNELS` のインデックスを指定 (Fp1, Fp2, F3, F4)
        frontal_indices = [0, 1, 2, 3]
        for idx in frontal_indices:
            ax.scatter(x[idx], y[idx], marker='o', c='black', s=40, zorder=4)
            ax.text(x[idx], y[idx] + 0.2, f"{SHOW_CHANNELS[idx]}",
                    color='black', fontsize=8, ha='center')
    else:
        # 何も強調しない場合でも、チャンネル名を小さく表示したいなら下のように書いてもOK
        # for idx, ch_name in enumerate(SHOW_CHANNELS):
        #     ax.text(x[idx], y[idx] + 0.15, ch_name,
        #             color='black', fontsize=6, ha='center')
        pass

    # 通常チャンネルの散布図
    ax.scatter(x, y, marker='o', c='b', s=15, zorder=3)

    # 頭部の外周
    circle = patches.Circle(xy=xy_center, radius=radius,
                            edgecolor="k", facecolor="none", zorder=4)
    ax.add_patch(circle)

    # 軸周りの装飾除去
    for loc, spine in ax.spines.items():
        spine.set_linewidth(0)
    ax.set_xticks([])
    ax.set_yticks([])

    # --------------------------------
    # 顔のパーツ（耳・鼻など）の描画
    # --------------------------------
    # 耳 (簡易的に楕円を2つ)
    circle_left = patches.Ellipse(xy=[0, 2], width=0.4, height=1.0, angle=0,
                                  edgecolor="k", facecolor="w", zorder=0)
    ax.add_patch(circle_left)
    circle_right = patches.Ellipse(xy=[4, 2], width=0.4, height=1.0, angle=0,
                                   edgecolor="k", facecolor="w", zorder=0)
    ax.add_patch(circle_right)

    # 鼻 (三角形のポリゴンで近似)
    nose_xy = [[1.6, 3.4], [2.0, 4.1], [2.4, 3.4]]
    polygon = patches.Polygon(xy=nose_xy, edgecolor="k",
                              facecolor="w", zorder=0)
    ax.add_patch(polygon)

    ax.set_xlim(-0.5, 4.5)
    ax.set_ylim(-0.5, 4.5)

    return ax